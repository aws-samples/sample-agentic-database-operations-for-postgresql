# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Alarm notifier Lambda: turns a database alarm's state change into an email.

Invoked by the EventBridge rule on this install's alarm-name prefix
(``<prefix>-db-``, see tools.aws.db_alarms) for every CloudWatch alarm state change, and
by the API for a test email (``{"test": true}``). It only reads and sends: it looks the
database up by the id in the alarm name, reads the alarm settings and the email sender
from the config table, and emails the deployment's notification address.

- An alarm email goes out when an alarm enters ALARM, a "resolved" email when it returns
  to OK after an alarm email was sent. INSUFFICIENT_DATA transitions send nothing.
- A flapping alarm is held to one alarm email per FLAP_WINDOW_S: the last send is kept in
  the config table (``alarm_sent:<alarm name>``).
- SES with an HTML body when a verified sender can reach the recipient
  (``db_alarms.choose_channel``); otherwise the notification topic, as plain text. An SES
  failure falls back to the topic, so an email is never lost to a sender problem.
- Alarms switched off in the console send nothing even if an alarm still exists (the
  reconcile that deletes it may not have run yet).

Bundled into the Tier 1 collector zip (same tools/ subset); stdlib + boto3 only.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone

import boto3

from tools.aws import db_alarms
from tools.resource_names import resource_prefix, table_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FLAP_WINDOW_S = 1800
PRODUCT = "DBOps Agent"
ENGINE_LABELS = {"aurora-postgresql": "Aurora PostgreSQL", "postgres": "RDS PostgreSQL",
                 "rds-postgresql": "RDS PostgreSQL"}
SCOPE_LABELS = {"writer": "the writer", "readers": "the most lagging reader", "database": "the database"}
COLOURS = {"critical": "#b42318", "warning": "#b54708", "resolved": "#067647", "test": "#3538cd"}

_ddb = None
_clients = {}


def _region() -> str:
    return os.getenv("AWS_REGION_NAME") or os.getenv("AWS_REGION") or "us-east-1"


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name, region_name=_region())
    return _clients[name]


def _table(key: str):
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_region())
    return _ddb.Table(table_name(key))


def _config(key: str) -> dict:
    try:
        return _table("config").get_item(Key={"config_key": key}).get("Item") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("config %s not read: %s", key, type(e).__name__)
        return {}


# ── Values as people read them ────────────────────────────────────────────────

def _duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


def format_value(alarm_id: str, value) -> str:
    if value is None:
        return "no recent value"
    v = float(value)
    if alarm_id == "collection_failed":
        return "every run in the last three intervals failed" if v >= 1 else "collecting again"
    if alarm_id == "xid_wraparound":
        return f"{v:.1f}% of the wraparound limit"
    if alarm_id == "connection_saturation":
        return f"{v:.0f}% of max_connections"
    if alarm_id == "long_transaction":
        return f"open for {_duration(v)}"
    if alarm_id == "blocked_queries":
        return f"{v:.0f} {'query' if v == 1 else 'queries'} waiting on locks"
    if alarm_id == "deadlocks":
        return f"{v:.0f} {'deadlock' if v == 1 else 'deadlocks'} in the interval"
    if alarm_id == "replica_lag":
        return f"{v / 1000:.1f} s behind"
    if alarm_id in ("cache_hit_ratio", "rollback_ratio"):
        return f"{v:.1f}%"
    return f"{v:g}"


def _reason(detail: dict) -> tuple:
    """(latest value, threshold) from the state's reasonData, when CloudWatch gave them."""
    raw = ((detail or {}).get("state") or {}).get("reasonData") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None, None
    points = data.get("recentDatapoints") or []
    return (points[-1] if points else None), data.get("threshold")


def _ascii(text: str, limit: int = 100) -> str:
    """An SNS subject: ASCII, one line, at most 100 characters."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[\r\n\t]+", " ", t).strip() or PRODUCT
    return t if len(t) <= limit else t[:limit - 3] + "..."


# ── The message ───────────────────────────────────────────────────────────────

def build_message(kind: str, alarm: dict, record: dict, value, threshold, when: str,
                  app_url: str = "") -> dict:
    """``{"subject", "text", "html"}``. ``kind``: alarm | resolved | test. Every value from
    the record or the event is escaped before it reaches the HTML."""
    name = record.get("display_name") or record.get("instance_id") or "a database"
    engine = ENGINE_LABELS.get(record.get("engine", ""), record.get("engine") or "PostgreSQL")
    region = record.get("region") or ""
    severity = alarm.get("severity", "warning")
    colour = COLOURS["resolved"] if kind == "resolved" else COLOURS["test"] if kind == "test" else COLOURS[severity]
    badge = {"alarm": f"ALARM · {severity.capitalize()}", "resolved": "RESOLVED", "test": "TEST"}[kind]
    label = alarm["label"]
    subject_tag = {"alarm": "Alarm", "resolved": "Resolved", "test": "Test"}[kind]
    subject = f"[{subject_tag}] {name}: {label}"
    lead = {
        "alarm": f"{label} on {name}.",
        "resolved": f"{label} on {name} is back to normal.",
        "test": f"This is a test of the {PRODUCT} alarm email. Real alarms look like this.",
    }[kind]
    thr = db_alarms.threshold_text(alarm["id"], float(threshold)) if threshold is not None else ""
    rows = [("Database", f"{name} · {engine}" + (f" · {region}" if region else "")),
            ("Measured on", SCOPE_LABELS.get(alarm.get("scope"), "the database")),
            ("Current value", format_value(alarm["id"], value)),
            ("Alarm when", thr),
            ("Changed at", when)]
    rows = [(k, v) for k, v in rows if v]
    steps = alarm.get("next_steps") or []
    meaning = alarm.get("meaning", "") if kind != "resolved" else \
        "The value is back inside the threshold. No action is needed unless it keeps returning."
    footer = (f"You receive this because alarms are on for the {resource_prefix()} deployment. "
              f"Switch all alarms, or single ones, off in Admin Console › Settings › Alarms.")

    text = [f"{badge}: {lead}", ""]
    text += [f"{k}: {v}" for k, v in rows]
    text += ["", "What it means", meaning]
    if steps and kind != "resolved":
        text += ["", "What to do"] + [f"- {s}" for s in steps]
    if app_url:
        text += ["", f"Open the operator app: {app_url}"]
    text += ["", footer]

    e = html.escape
    row_html = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#475467;white-space:nowrap;vertical-align:top">{e(k)}</td>'
        f'<td style="padding:6px 0;color:#101828;font-weight:500">{e(v)}</td></tr>' for k, v in rows)
    steps_html = "" if (not steps or kind == "resolved") else (
        '<h3 style="margin:20px 0 6px;font-size:14px;color:#101828">What to do</h3><ul style="margin:0;'
        'padding-left:20px;color:#344054;font-size:14px;line-height:1.5">'
        + "".join(f'<li style="margin:4px 0">{e(s)}</li>' for s in steps) + "</ul>")
    button = "" if not app_url else (
        f'<p style="margin:24px 0 0"><a href="{e(app_url, quote=True)}" style="display:inline-block;'
        f'background:{colour};color:#ffffff;text-decoration:none;padding:10px 16px;border-radius:8px;'
        f'font-size:14px;font-weight:600">Open the operator app</a></p>')
    body = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f2f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #eaecf0">
<tr><td style="background:{colour};padding:14px 24px;color:#ffffff;font-size:12px;font-weight:700;letter-spacing:.06em">{e(badge)}</td></tr>
<tr><td style="padding:24px">
<p style="margin:0 0 4px;color:#475467;font-size:13px">{e(PRODUCT)}</p>
<h1 style="margin:0 0 8px;font-size:20px;line-height:1.3;color:#101828">{e(label)}</h1>
<p style="margin:0 0 16px;color:#344054;font-size:15px">{e(lead)}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="font-size:14px;border-top:1px solid #eaecf0;padding-top:8px;width:100%">{row_html}</table>
<h3 style="margin:20px 0 6px;font-size:14px;color:#101828">What it means</h3>
<p style="margin:0;color:#344054;font-size:14px;line-height:1.5">{e(meaning)}</p>
{steps_html}{button}
</td></tr>
<tr><td style="padding:16px 24px;background:#f9fafb;color:#667085;font-size:12px;line-height:1.5;border-top:1px solid #eaecf0">{e(footer)}</td></tr>
</table></td></tr></table></body></html>"""
    return {"subject": subject, "text": "\n".join(text), "html": body}


# ── Delivery ──────────────────────────────────────────────────────────────────

def _ses_ready(sender: dict, recipient: str) -> dict:
    ses = _client("sesv2")

    def verified(addr):
        try:
            return bool(ses.get_email_identity(EmailIdentity=addr).get("VerifiedForSendingStatus"))
        except Exception:  # noqa: BLE001 - not found / no access: not verified
            return False
    addr = (sender or {}).get("from_address", "")
    try:
        acct = ses.get_account()
        sending, production = bool(acct.get("SendingEnabled")), bool(acct.get("ProductionAccessEnabled"))
    except Exception:  # noqa: BLE001
        sending = production = False
    return db_alarms.choose_channel(
        sender, bool(addr) and verified(addr), sending, production, recipient,
        bool(recipient) and not production and verified(recipient),
        os.getenv("NOTIFICATION_TOPIC_ARN", ""))


def send(message: dict) -> str:
    """Send ``message``; returns the channel used ('ses' | 'sns' | 'none')."""
    recipient = (os.getenv("ALARM_EMAIL_RECIPIENT") or "").strip()
    sender = _config("email_sender")
    choice = _ses_ready(sender, recipient)
    if choice["channel"] == "ses":
        frm = sender["from_address"]
        if sender.get("from_name"):
            frm = f'{sender["from_name"]} <{frm}>'
        try:
            _client("sesv2").send_email(
                FromEmailAddress=frm, Destination={"ToAddresses": [recipient]},
                Content={"Simple": {"Subject": {"Data": message["subject"], "Charset": "UTF-8"},
                                    "Body": {"Text": {"Data": message["text"], "Charset": "UTF-8"},
                                             "Html": {"Data": message["html"], "Charset": "UTF-8"}}}})
            return "ses"
        except Exception as e:  # noqa: BLE001 - fall back to the topic rather than lose it
            logger.warning("SES send failed (%s); falling back to the topic", type(e).__name__)
    topic = os.getenv("NOTIFICATION_TOPIC_ARN", "")
    if not topic:
        logger.warning("no notification topic: alarm email not sent")
        return "none"
    _client("sns").publish(TopicArn=topic, Subject=_ascii(message["subject"]), Message=message["text"])
    return "sns"


def _recently_sent(name: str) -> bool:
    item = _config(f"alarm_sent:{name}")
    try:
        last = datetime.fromisoformat(str(item.get("sent_at")))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < FLAP_WINDOW_S


def _alarm_was_sent(name: str) -> bool:
    return bool(_config(f"alarm_sent:{name}").get("sent_at"))


def _mark_sent(name: str, kind: str) -> None:
    try:
        tbl = _table("config")
        if kind == "alarm":
            tbl.put_item(Item={"config_key": f"alarm_sent:{name}",
                               "sent_at": datetime.now(timezone.utc).isoformat()})
        else:
            tbl.delete_item(Key={"config_key": f"alarm_sent:{name}"})
    except Exception as e:  # noqa: BLE001
        logger.warning("alarm send marker not written: %s", type(e).__name__)


def _when(detail: dict, event: dict) -> str:
    raw = ((detail or {}).get("state") or {}).get("timestamp") or event.get("time") or ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace("+0000", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(raw)


def handler(event, context=None):
    app_url = (os.getenv("OPERATOR_APP_URL") or "").strip()
    if event.get("test"):
        alarm = db_alarms.BY_ID["xid_wraparound"]
        rec = {"display_name": "example-database", "engine": "aurora-postgresql", "region": _region()}
        msg = build_message("test", alarm, rec, 48.2, 46.57,
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), app_url)
        return {"sent_via": send(msg)}

    detail = event.get("detail") or {}
    name = detail.get("alarmName", "")
    parsed = db_alarms.parse_alarm_name(name)
    if not parsed:
        return {"skipped": "not a database alarm"}
    alarm_id, instance_id = parsed
    new = (detail.get("state") or {}).get("value")
    old = (detail.get("previousState") or {}).get("value")
    if new == "ALARM" and old != "ALARM":
        kind = "alarm"
    elif new == "OK" and old == "ALARM":
        kind = "resolved"
    else:
        return {"skipped": f"{old} -> {new}"}

    settings = db_alarms.resolve_settings(_config(db_alarms.SETTINGS_KEY))
    if not settings["enabled"] or not settings["alarms"].get(alarm_id):
        return {"skipped": "switched off"}
    try:
        record = _table("instances").get_item(Key={"instance_id": instance_id}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.warning("record %s not read: %s", instance_id, type(e).__name__)
        record = None
    if not record:
        return {"skipped": "database not found"}
    if kind == "alarm" and _recently_sent(name):
        return {"skipped": "flapping: an alarm email was sent recently"}
    if kind == "resolved" and not _alarm_was_sent(name):
        return {"skipped": "no alarm email to resolve"}

    value, threshold = _reason(detail)
    msg = build_message(kind, db_alarms.BY_ID[alarm_id], record, value, threshold, _when(detail, event), app_url)
    via = send(msg)
    if via != "none":
        _mark_sent(name, kind)
    logger.info("alarm %s %s for %s sent via %s", alarm_id, kind, instance_id, via)
    return {"sent_via": via, "kind": kind}
