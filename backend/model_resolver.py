# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-time model resolution for model governance.

Resolves the effective Bedrock model for a request from the governance store with
precedence **user selection → space default → space-admin tier default → account
default → env default**, only
ever returning a model still in the applicable allowlist (a dangling reference falls
through to the next candidate). It always resolves to *some* valid id.

The resolved id is published on a ``ContextVar`` so the supervisor factory AND every
sub-agent (each builds its own ``BedrockModel`` per call) use the same model without
threading it through tool signatures. Sub-agents call ``effective_model_id()``.

Fail-soft: if the governance store is unavailable, resolution returns the env default
so a store hiccup never breaks a chat turn.
"""

import os
import logging
import random
import time
from contextvars import ContextVar

logger = logging.getLogger("model-resolver")

# Set per request in /invocations; read by sub-agents when they build their model.
CURRENT_MODEL_ID: ContextVar[str] = ContextVar("current_model_id", default="")
# Caller identity for the current request; used by knowledge composition
# (get_runbooks) to apply the per-user knowledge layer.
CURRENT_USER_ID: ContextVar[str] = ContextVar("current_user_id", default="")
# Resolved space for the current request; read by space-scoped agent tools
# (e.g. list_monitored_databases) so a tool can filter to the caller's space
# without threading it through every tool signature. Empty when unknown.
CURRENT_SPACE_ID: ContextVar[str] = ContextVar("current_space_id", default="")
# Aurora role focus for the current chat request: "" (entire cluster, default),
# "writer", "reader", or "instance:<db-instance-id>" (one specific member).
# Set from the validated /invocations payload; read by pg_client.get_connection
# to route SQL connections to the cluster's reader endpoint (reader focus) or a
# member's own endpoint (instance focus) — so pg_stat_* views genuinely reflect
# the focused member instead of silently reporting the writer.
CURRENT_ROLE_FOCUS: ContextVar[str] = ContextVar("current_role_focus", default="")
# The request's database aliases: {"db-1": "<registry id>", ...}. /invocations sets it
# alongside CURRENT_SPACE_ID, from the same records the server-built [DATABASE SCOPE]
# block lists, so the model reads and writes short aliases and never a UUID (the model
# guardrail's AWS_SECRET_KEY filter garbled pieces of UUIDs). None when the request has
# no server-built scope. The mapping lives here only, never in model-visible text; the
# tool resolvers (tools/aws/db_aliases.py) read it. Treat the value as read-only.
CURRENT_DB_ALIASES: ContextVar = ContextVar("current_db_aliases", default=None)

# The whole chat SESSION's alias -> id mapping (every database that has appeared in the
# session, not just this turn's selection). Aliases are stable for the session: assigned
# the first time a database appears and never reassigned or reused. CURRENT_DB_ALIASES
# above is only this turn's in-scope subset — the one the tools resolve — while this
# full map lets a tool tell a known-but-out-of-scope alias ("db-2 is not in the current
# scope") from an unknown one. None outside a session-scoped request.
CURRENT_SESSION_ALIASES: ContextVar = ContextVar("current_session_aliases", default=None)


def _env_default() -> str:
    return os.getenv("AGENT_MODEL_ID", "global.anthropic.claude-haiku-5-5")


def account_default_model(catalog: dict) -> tuple:
    """The effective account-level default: ``(model_id, source)``.

    ``source`` is ``"account"`` when an admin has explicitly chosen one, or
    ``"deployment"`` when it falls back to ``AGENT_MODEL_ID`` — the model the
    ``AgentModelId`` CloudFormation parameter was deployed with.

    IMPORTANT: a stored default that has since been DISABLED is not in force — request
    resolution skips it — so it must not be shown as the default either. When
    the stored default is not in the catalog's enabled set, we fall through to the real
    effective default (``AGENT_MODEL_ID`` if it is enabled, else the first enabled model),
    tagging the source ``deployment``/``fallback`` so the console shows what a request
    will actually use rather than a disabled model as "Default".

    Every surface that needs the account default MUST come through here: the admin
    console, the space and space-admin views, and request-time resolution. Deriving it
    separately in any of them lets them disagree, and a console that displays a default
    nothing else honors is worse than one that displays none — an admin sees the model
    they want already selected and so never stores a choice.
    """
    enabled = {mid for mid, m in (catalog.get("models") or {}).items() if m.get("enabled")}
    stored = (catalog.get("account_default_model") or "").strip()
    # No enablement info in the catalog (older/empty record): keep the historical
    # behaviour so a bare catalog still reports its stored/deployment default.
    if stored and (not enabled or stored in enabled):
        return stored, "account"
    # The stored default is gone/disabled (or none was set): report the real fallback.
    env = _env_default()
    if not enabled or env in enabled:
        return env, "deployment"
    return sorted(enabled)[0], "fallback"


def effective_model_id() -> str:
    """Model id for the current request context (contextvar), or the env default.

    Lets a per-request resolved model flow into sub-agents and any per-call
    ``BedrockModel`` construction without changing tool signatures.
    """
    return CURRENT_MODEL_ID.get() or _env_default()


def resolve_model(space_id: str = "", user_selection: str = "") -> dict:
    """Resolve the effective model. Returns ``{"model_id", "source"}``.

    Precedence (first valid wins): user selection → space default → space-admin
    tier default → account default → env default. "Valid" = survives the
    disable-based cascade (account-enabled minus admin-tier and space disables).
    Never raises.
    """
    try:
        from tools.aws.governance_store import governance_registry
        catalog = governance_registry.get_model_catalog()
        allow = {mid for mid, m in catalog.get("models", {}).items() if m.get("enabled")}
        account_default = catalog.get("account_default_model", "")
    except Exception as e:
        logger.warning(f"resolve_model: catalog unavailable ({e}); using env default")
        return {"model_id": _env_default(), "source": "env"}

    # Cascade: account allowlist → space-admin tier disables → space disables.
    # Anything enabled above flows down enabled-by-default; each layer only
    # ever narrows (disable-based), so a new account enable cascades through
    # untouched layers automatically.
    space_default = ""
    admin_default = ""
    selectable = set(allow)
    if space_id:
        try:
            from tools.aws.spaces_client import spaces_registry
            space = spaces_registry.get_space(space_id) or {}
            mg = space.get("model_governance") or {}
            space_default = mg.get("default_model", "") or ""
            try:
                from tools.aws.space_admins import managing_admin_subs
                for sub in sorted(managing_admin_subs(space_id, space.get("created_by", ""))):
                    rec = governance_registry.get_admin_governance(sub)
                    selectable -= set(rec.get("disabled_models") or [])
                    # Tier default: first (sorted-sub) managing admin with one set —
                    # deterministic when a space has co-admins.
                    if not admin_default and rec.get("default_model"):
                        admin_default = rec["default_model"]
            except Exception as e:
                logger.warning(f"resolve_model: admin tier unavailable ({e})")
            if mg.get("disabled_models") is not None:
                selectable -= set(mg.get("disabled_models") or [])
            elif mg.get("enabled_models"):
                # Legacy allowlist records (pre-cascade): still honored until rewritten.
                selectable &= set(mg["enabled_models"])
        except Exception as e:
            logger.warning(f"resolve_model: space {space_id} unavailable ({e})")

    if user_selection and user_selection in selectable:
        return {"model_id": user_selection, "source": "user"}
    if space_default and space_default in selectable:
        return {"model_id": space_default, "source": "space"}
    if admin_default and admin_default in selectable:
        return {"model_id": admin_default, "source": "space-admin"}
    if account_default and account_default in selectable:
        # Admin-level default is a deliberate account choice — honor it while it
        # survives the cascade (a layer that disabled it opted out of it).
        return {"model_id": account_default, "source": "account"}
    # The deployment's AgentModelId is an explicit operator choice too, so it outranks
    # "whichever model happens to sort first". Without this, a fresh install where no
    # layer has stored a default falls through to sorted(selectable)[0], so the first
    # request runs an arbitrary model — alphabetically, a Fable/Claude-3 style id beats
    # the intended Haiku — while the admin console shows AgentModelId as though it were
    # in force. That is the out-of-the-box path for every deployment, not an edge case.
    env_default = _env_default()
    if env_default in selectable:
        return {"model_id": env_default, "source": "deployment"}
    # Nothing configured is usable: pick deterministically so repeated requests at
    # least agree with each other, and label it honestly as a fallback.
    if selectable:
        return {"model_id": sorted(selectable)[0], "source": "cascade"}
    if account_default:
        return {"model_id": account_default, "source": "account"}
    return {"model_id": env_default, "source": "env"}


# ── Temperature capability (Bedrock sampling-param governance) ────────────────
# Claude Opus 4.7+ and the next-gen line (Opus/Sonnet/Haiku 5) dropped the
# temperature/top_p/top_k sampling params: Bedrock ConverseStream returns a 400
# ValidationException if a non-default value is sent, and Anthropic's guidance is
# to omit them and steer via prompting. We only ever set `temperature`, so for
# those families we omit just that and keep temperature=0 for models that still
# accept it (Haiku 4.5, Sonnet 4.5, and non-Anthropic providers like Nova).

# Version markers of Claude families that reject sampling params. Matched as a
# substring of the lowercased inference-profile id, so region/profile prefixes
# (global.*/us.*) and version suffixes don't matter.
_NO_TEMPERATURE_MARKERS = (
    "opus-4-8", "opus-4-7",
    "opus-5", "sonnet-5", "haiku-5", "claude-5",
)

# Models observed at runtime to reject temperature (learned by the retry net in
# app.py) so later builds for the same id omit it without another 400 — a safety
# net for any family the static markers above don't yet cover.
_TEMPERATURE_UNSUPPORTED: set = set()


def mark_temperature_unsupported(model_id: str) -> None:
    """Record at runtime that a model rejects the temperature param, so the next
    build omits it (fed by the ConverseStream retry net)."""
    if model_id:
        _TEMPERATURE_UNSUPPORTED.add(model_id)


def model_supports_temperature(model_id: str) -> bool:
    """Whether this model accepts a `temperature` value on Bedrock ConverseStream.

    False for the Claude families that deprecated sampling params (Opus 4.7+ and
    the next-gen 5 line) or any id learned unsupported at runtime; True otherwise.
    Pure/stdlib — unit-testable without strands."""
    mid = model_id or ""
    if mid in _TEMPERATURE_UNSUPPORTED:
        return False
    low = mid.lower()
    return not any(marker in low for marker in _NO_TEMPERATURE_MARKERS)


# ── Reasoning controls per surface ────────────────────────────────────────────
#
# Current Claude models reason ("think") adaptively by default, and the reasoning
# tokens count against max_tokens. The two knobs Anthropic exposes for this are the
# `output_config.effort` level (how much reasoning and how terse the output is) and
# the `thinking` block (`{"type": "disabled"}` turns reasoning off outright). Both go
# through Bedrock's additional model request fields, in Converse and InvokeModel alike.
#
# The product uses them per SURFACE, not per model, so a new model version only needs
# its capability markers added here:
#   - chat: the orchestrator routes questions and hands deep work to investigations,
#     so it runs at a lower effort for latency;
#   - investigation: the deep-dive surface runs at a higher effort;
#   - helper: the small utility calls (follow-up pills, scan-comparison summary) want
#     a short, deterministic text answer within a small token budget, so reasoning is
#     off and effort is lowest. A reasoning model left at its default would spend the
#     whole budget thinking and return no text at all.
# Each level is overridable through CHAT_EFFORT / INVESTIGATION_EFFORT / HELPER_EFFORT
# so a deployment can retune without a code change.

SURFACE_CHAT = "chat"
SURFACE_INVESTIGATION = "investigation"
SURFACE_HELPER = "helper"
_SURFACES = (SURFACE_CHAT, SURFACE_INVESTIGATION, SURFACE_HELPER)

# The surface of the current request: set by /invocations for a chat turn or an
# investigation stream, read by build_bedrock_model so the sub-agents a surface
# spawns inherit its effort without threading it through every tool signature.
CURRENT_SURFACE: ContextVar[str] = ContextVar("current_surface", default=SURFACE_CHAT)

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_DEFAULTS = {
    SURFACE_CHAT: "low",
    SURFACE_INVESTIGATION: "high",
    SURFACE_HELPER: "low",
}
_EFFORT_ENV = {
    SURFACE_CHAT: "CHAT_EFFORT",
    SURFACE_INVESTIGATION: "INVESTIGATION_EFFORT",
    SURFACE_HELPER: "HELPER_EFFORT",
}

# Claude families that accept `output_config.effort`: Opus 4.5 onwards, Sonnet 4.6
# onwards and the whole 5 line. Haiku 4.5 and older models reject it with a 400
# ("This model does not support the effort parameter"). Substring match on the
# lowercased inference-profile id, like the temperature markers above.
_EFFORT_MARKERS = (
    "opus-4-5", "opus-4-6", "opus-4-7", "opus-4-8",
    "sonnet-4-6", "sonnet-4-7", "sonnet-4-8",
    "opus-5", "sonnet-5", "haiku-5", "claude-5",
)
# Claude models that accept the `thinking` request block (3.7 onwards). Older Claude
# ids and every non-Anthropic provider do not.
_NO_THINKING_MARKERS = ("claude-3-", "claude-v2", "claude-instant")

# Ids learned at runtime to reject a reasoning control (fed by the retry nets), so
# the next build omits it without another 400.
_EFFORT_UNSUPPORTED: set = set()
_THINKING_UNSUPPORTED: set = set()


def mark_effort_unsupported(model_id: str) -> None:
    """Record at runtime that a model rejects `output_config.effort`."""
    if model_id:
        _EFFORT_UNSUPPORTED.add(model_id)


def mark_thinking_unsupported(model_id: str) -> None:
    """Record at runtime that a model rejects the `thinking` request block."""
    if model_id:
        _THINKING_UNSUPPORTED.add(model_id)


def model_supports_effort(model_id: str) -> bool:
    """Whether this model accepts `output_config.effort` (see _EFFORT_MARKERS)."""
    mid = model_id or ""
    if mid in _EFFORT_UNSUPPORTED:
        return False
    low = mid.lower()
    return any(marker in low for marker in _EFFORT_MARKERS)


def model_supports_thinking_control(model_id: str) -> bool:
    """Whether this model accepts the `thinking` request block (Claude 3.7 onwards)."""
    mid = model_id or ""
    if mid in _THINKING_UNSUPPORTED:
        return False
    low = mid.lower()
    if "anthropic" not in low and "claude" not in low:
        return False
    return not any(marker in low for marker in _NO_THINKING_MARKERS)


def surface_effort(surface: str) -> str:
    """The effort level for a surface: its env override when valid, else the default."""
    if surface not in _SURFACES:
        raise ValueError(f"unknown surface {surface!r}")
    override = (os.getenv(_EFFORT_ENV[surface]) or "").strip().lower()
    if override in _EFFORT_LEVELS:
        return override
    if override:
        logger.warning("%s=%r is not one of %s; using %r", _EFFORT_ENV[surface], override,
                       "/".join(_EFFORT_LEVELS), _EFFORT_DEFAULTS[surface])
    return _EFFORT_DEFAULTS[surface]


def surface_model_kwargs(model_id: str, surface: str) -> dict:
    """The additional Bedrock request fields a surface sends to this model.

    Only fields the model accepts are included, so the result is safe to merge into
    an InvokeModel body or to pass as Converse's additional request fields. An
    unsupported model gets ``{}`` and runs at the provider's defaults.
    """
    fields: dict = {}
    if surface == SURFACE_HELPER and model_supports_thinking_control(model_id):
        fields["thinking"] = {"type": "disabled"}
    if model_supports_effort(model_id):
        fields["output_config"] = {"effort": surface_effort(surface)}
    return fields


def reasoning_summary(model_id: str, surface: str) -> dict:
    """What a surface's model call runs with, for the agent trace: the surface, the effort
    level sent (``None`` when the model takes no effort parameter) and the thinking mode
    (``"disabled"`` when the call turns it off, ``"model default"`` when the call leaves it
    to the model, ``"not supported"`` for a model without the control). Derived from
    surface_model_kwargs, so the trace shows exactly what is sent."""
    fields = surface_model_kwargs(model_id, surface)
    if "thinking" in fields:
        thinking = fields["thinking"].get("type", "")
    elif model_supports_thinking_control(model_id):
        thinking = "model default"
    else:
        thinking = "not supported"
    return {"surface": surface, "effort": (fields.get("output_config") or {}).get("effort"),
            "thinking": thinking}


def helper_model_id() -> str:
    """The model the small utility calls run on: the deployment default, regardless
    of the request's user-selected model, so a helper's token budget and prompt are
    tuned once against one model. ``CHAT_FOLLOWUPS_MODEL_ID`` overrides it."""
    return (os.getenv("CHAT_FOLLOWUPS_MODEL_ID") or "").strip() or _env_default()


def unsupported_reasoning_param(exc: Exception) -> str:
    """Which reasoning control a Bedrock ValidationException rejects: ``"effort"``,
    ``"thinking"`` or ``""`` when the error is about something else."""
    s = str(exc).lower()
    if "validation" not in s and "not support" not in s and "invalid" not in s:
        return ""
    if "effort" in s or "output_config" in s:
        return "effort"
    if "thinking" in s:
        return "thinking"
    return ""


class GuardrailConfigError(RuntimeError):
    """Raised when guardrail enforcement is ON but no guardrail is configured.

    Signals a FAIL-CLOSED decision: rather than silently invoking the LLM with no
    prompt-attack / PII protection, refuse the invocation. Only raised when
    ``guardrail_enforcement_enabled()`` is true AND ``GUARDRAIL_ID``/``VERSION`` are
    missing."""


def guardrail_enforcement_enabled() -> bool:
    """True when the guardrail is REQUIRED (fail-closed on missing config).

    Rollout-safe: defaults OFF, so behavior is byte-identical to today until an
    operator opts in via ``GUARDRAIL_ENFORCE``, which
    should only be flipped once a deploy-time check confirms the guardrail resource
    exists in the environment, so the flip cannot cause an LLM-path outage. When ON
    and the guardrail *is* configured, behavior is unchanged."""
    val = (os.getenv("GUARDRAIL_ENFORCE", "") or "").strip().lower()
    return val in ("1", "true", "yes", "on", "require", "required", "closed")


def guardrail_kwargs_from_env() -> dict:
    """Bedrock guardrail kwargs from env, or ``{}`` (a strict no-op) — unless
    enforcement is enabled and the guardrail is unconfigured, in which case it
    FAILS CLOSED by raising ``GuardrailConfigError``.

    Returns guardrail config ONLY when BOTH ``GUARDRAIL_ID`` and ``GUARDRAIL_VERSION``
    are set — which matches Strands' own gate (it emits ``guardrailConfig`` on the
    Converse request only when both are present). With the env unset AND enforcement
    OFF (the default), behavior is byte-identical to no guardrail: the feature is safe
    to ship before any guardrail resource exists (fail-soft).
    With enforcement ON and the env unset, the invocation is refused (fail-closed)
    instead of silently disabling prompt-attack detection + PII redaction.

    This is the RESPONSE-screening half. The guardrail these ids name carries content
    filters on output only, with every input strength at NONE, so the conversation
    history replayed on each Converse request is not re-assessed. User input is screened
    separately and one turn at a time by ``screen_user_input`` below, which is where
    prompt-attack detection lives.

    The actual policies live on the guardrail *resource*
    (`infrastructure/guardrail.yaml`); here we only attach its identity + the
    client-side behavior on a blocking intervention. PII ANONYMIZE masks inline
    server-side and streams through — it does not trip the redact-output replacement
    (which only fires on an actual block). Pure/stdlib so it is unit-testable without
    strands."""
    gid = (os.getenv("GUARDRAIL_ID") or "").strip()
    gver = (os.getenv("GUARDRAIL_VERSION") or "").strip()
    if not gid or not gver:
        if guardrail_enforcement_enabled():
            raise GuardrailConfigError(
                "Guardrail enforcement is enabled (GUARDRAIL_ENFORCE) but "
                "GUARDRAIL_ID/GUARDRAIL_VERSION are not set. Refusing the LLM "
                "invocation (fail-closed) rather than running without response "
                "screening and PII redaction."
            )
        return {}
    return {
        "guardrail_id": gid,
        "guardrail_version": gver,
        "guardrail_trace": os.getenv("GUARDRAIL_TRACE", "enabled"),
        # sync = evaluate before emitting, so a blocked/PII token is never streamed
        # then retracted. Safer for PII/content at the cost of some streaming latency.
        "guardrail_stream_processing_mode": os.getenv("GUARDRAIL_STREAM_MODE", "sync"),
        # On a blocking intervention, replace content with a clear message
        # instead of surfacing partial/garbled output.
        "guardrail_redact_input": True,
        "guardrail_redact_input_message": "[Blocked by a safety guardrail.]",
        "guardrail_redact_output": True,
        "guardrail_redact_output_message": "[Response blocked by a safety guardrail.]",
    }


def guardrail_invoke_kwargs_from_env() -> dict:
    """Guardrail kwargs for a RAW ``bedrock-runtime`` call, or ``{}``.

    ``guardrail_kwargs_from_env`` above returns Strands' ``BedrockModel`` keyword shape.
    A few paths call ``bedrock.invoke_model`` directly rather than going through
    ``build_bedrock_model`` — the report comparison summary, and the Batch scan's
    analysis leg — and those need the API's own parameter names. Same environment, same
    both-or-nothing rule, same fail-closed behaviour, so a direct call cannot quietly
    end up being the one unguarded path.
    """
    gid = (os.getenv("GUARDRAIL_ID") or "").strip()
    gver = (os.getenv("GUARDRAIL_VERSION") or "").strip()
    if not gid or not gver:
        if guardrail_enforcement_enabled():
            raise GuardrailConfigError(
                "Guardrail enforcement is enabled (GUARDRAIL_ENFORCE) but "
                "GUARDRAIL_ID/GUARDRAIL_VERSION are not set. Refusing the LLM "
                "invocation (fail-closed) rather than running without response "
                "screening and PII redaction."
            )
        return {}
    return {"guardrailIdentifier": gid, "guardrailVersion": gver}


BLOCKED_INPUT_FALLBACK = "Your request was blocked by a safety guardrail. Please rephrase."

# A soft, retryable refusal used ONLY when the screen is failed closed under
# GUARDRAIL_ENFORCE after retries (the safety service is throttling, not a policy block).
GUARDRAIL_UNAVAILABLE_MESSAGE = (
    "The safety check is temporarily unavailable, so this message was not sent. "
    "Please try again in a moment."
)

# Retry budget for a throttled ApplyGuardrail call. Small on purpose: a blip clears in a
# retry or two, so worst-case added latency is a few hundred ms; a sustained throttle then
# either fails open (default) or, under GUARDRAIL_ENFORCE, fails closed. Module-level so a
# test can drop the backoff to zero.
_SCREEN_MAX_ATTEMPTS = 3
_SCREEN_BACKOFF_BASE = 0.2  # seconds; grows 0.2s, 0.4s between attempts, plus jitter

# Substrings that identify a throttle across botocore ClientError codes and modelled
# exception types (checked case-insensitively against the type name, the error Code, and
# the message).
_THROTTLE_TOKENS = ("throttl", "toomanyrequests", "provisionedthroughputexceeded", "slowdown")


def _is_throttle(exc) -> bool:
    """True when an exception is a rate/throughput throttle rather than another failure."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error", {}) or {}).get("Code", "") or ""
    hay = f"{type(exc).__name__} {code} {exc}".lower()
    return any(tok in hay for tok in _THROTTLE_TOKENS)

# Cached bedrock-runtime client for input screening. Built on first use so importing
# this module still needs nothing beyond the standard library.
_input_guardrail_client = None


def _get_input_guardrail_client():
    global _input_guardrail_client
    if _input_guardrail_client is None:
        import boto3
        _input_guardrail_client = boto3.client(
            "bedrock-runtime",
            # The guardrail is a regional resource created by this deployment's own
            # stack, so it resolves in the deployment's region unless an operator has
            # pointed model traffic somewhere else with BEDROCK_REGION.
            region_name=(os.getenv("BEDROCK_REGION")
                         or os.getenv("AWS_REGION")
                         or "us-east-1"),
        )
    return _input_guardrail_client


def input_guardrail_from_env() -> tuple:
    """``(id, version)`` of the guardrail that screens one user turn, or ``("", "")``.

    Same both-or-nothing and fail-closed rules as the model-attached pair above: a
    half-configured guardrail is treated as absent, and an absent one is refused
    outright when ``GUARDRAIL_ENFORCE`` is on rather than silently skipping the screen.
    """
    gid = (os.getenv("GUARDRAIL_INPUT_ID") or "").strip()
    gver = (os.getenv("GUARDRAIL_INPUT_VERSION") or "").strip()
    if not gid or not gver:
        if guardrail_enforcement_enabled():
            raise GuardrailConfigError(
                "Guardrail enforcement is enabled (GUARDRAIL_ENFORCE) but "
                "GUARDRAIL_INPUT_ID/GUARDRAIL_INPUT_VERSION are not set. Refusing the "
                "turn (fail-closed) rather than letting it reach the agent unscreened."
            )
        return "", ""
    return gid, gver


def screen_user_input(text: str) -> tuple:
    """Assess a single user turn, returning ``(blocked, message)``.

    This is the input half of the guardrail arrangement, and it is deliberately a
    separate call rather than a filter on the Converse request. A Converse request
    carries the whole replayed conversation, and a guardrail assesses the request it is
    handed, so an input filter attached to the model re-reads every earlier turn on
    every request: one turn above the threshold then blocks every later request in the
    session whatever it says, and no rewording clears it, because the text driving the
    decision is in the history rather than in the new message. Assessing the turn on its
    own keeps the decision proportional to what the user actually typed, and gives the
    caller a refusal it can act on *before* the turn reaches conversation memory — which
    is what stops a refused turn from influencing later ones.

    ``message`` carries the guardrail's own ``BlockedInputMessaging`` when it blocks,
    so the wording stays owned by the guardrail resource rather than duplicated here.

    A throttle is retried a few times with jittered backoff, since a brief spike clears
    in a retry or two. What happens when it does NOT clear depends on the mode:

      * Default (``GUARDRAIL_ENFORCE`` off): a sustained throttle is logged and allowed
        through, byte-identical to before, so a Bedrock outage cannot take down chat.
      * Enforced (``GUARDRAIL_ENFORCE`` on): a sustained throttle fails CLOSED with a
        soft, retryable message, because an operator who required screening would rather
        refuse a turn than let it through unscreened. This only bites during a real,
        sustained outage: the caller-supplied prompt is length-capped upstream, so no one
        turn can drive the account past the ApplyGuardrail quota and manufacture the
        condition.

    A non-throttle exception (a malformed response, an unexpected error) is logged and
    allowed through in either mode, so a single odd failure cannot become an outage; only
    a *sustained throttle* fails closed. Everything not depending on this call stays in
    force regardless: the model-attached guardrail still screens the response, and the
    space scoping, SQL catalogue guard and host allowlist are enforced in application code
    below the model.
    """
    if not text or not text.strip():
        return False, ""
    gid, gver = input_guardrail_from_env()
    if not gid:
        return False, ""
    response = None
    attempts = max(1, _SCREEN_MAX_ATTEMPTS)
    for attempt in range(attempts):
        try:
            response = _get_input_guardrail_client().apply_guardrail(
                guardrailIdentifier=gid,
                guardrailVersion=gver,
                source="INPUT",
                content=[{"text": {"text": text}}],
            )
            break
        except Exception as e:
            throttled = _is_throttle(e)
            if throttled and attempt < attempts - 1:
                # Transient busy-ness: back off a little and retry.
                time.sleep(_SCREEN_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, _SCREEN_BACKOFF_BASE))
                continue
            if throttled and guardrail_enforcement_enabled():
                # Sustained throttle under enforcement: fail closed with a soft, retryable
                # message (not a policy block, so not the guardrail's block wording).
                logger.warning(
                    "input guardrail throttled after %d attempt(s); refusing the turn "
                    "(GUARDRAIL_ENFORCE)", attempt + 1,
                )
                return True, GUARDRAIL_UNAVAILABLE_MESSAGE
            logger.warning("input guardrail screening failed (%s); allowing the turn", e)
            return False, ""
    if response.get("action") != "GUARDRAIL_INTERVENED":
        return False, ""
    outputs = response.get("outputs") or []
    message = (outputs[0].get("text") if outputs else "") or BLOCKED_INPUT_FALLBACK
    # Named policies only, never the turn itself: the text is the thing that just failed
    # a safety check, and this line goes to CloudWatch.
    policies = sorted({
        key.replace("Policy", "")
        for assessment in (response.get("assessments") or [])
        for key in assessment
    })
    logger.info("input guardrail blocked a turn (policies=%s)", ",".join(policies) or "unknown")
    return True, message


def build_bedrock_model(model_id: str = "", max_tokens: int = 32000, surface: str = ""):
    """Construct a BedrockModel for the resolved id, applying temperature=0 ONLY
    when the model supports it (Opus 4.7+/next-gen reject it), the surface's effort
    level where the model accepts one, and attaching the response-screening Bedrock
    Guardrail when configured (fail-soft via env). The single place the temperature,
    effort and guardrail decisions are made, so every agent + sub-agent stays
    consistent. ``surface`` defaults to the current request's surface
    (CURRENT_SURFACE), which is how a sub-agent inherits its parent's effort."""
    from strands.models.bedrock import BedrockModel
    mid = model_id or effective_model_id()
    surface = surface or CURRENT_SURFACE.get()
    kwargs = dict(
        model_id=mid,
        max_tokens=max_tokens,
        # fall back to the DEPLOYMENT region, not a hardcoded us-east-1, so a
        # non-us-east-1 deployment does not silently route operator prompts, replayed
        # history and tool results (query text, DB usernames, client addresses) to a
        # region the customer did not choose. Matches the guardrail region resolved a few
        # lines below and every other Bedrock caller. BEDROCK_REGION still overrides.
        region_name=os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1",
    )
    if model_supports_temperature(mid):
        kwargs["temperature"] = 0
    extra = surface_model_kwargs(mid, surface)
    if extra:
        kwargs["additional_request_fields"] = extra
    kwargs.update(guardrail_kwargs_from_env())
    return BedrockModel(**kwargs)
