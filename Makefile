# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# One deploy model: the nested stack at infrastructure/master.yaml.
#
#   make deploy    interactive - prompts once, saves deploy.config, then deploys
#   make deploy-all  same steps, no prompts (reads deploy.config or the environment)
#
# Every setting comes from deploy.config, which `make deploy` writes on first run.
# Override any of them on the command line: `make deploy-all RESOURCE_PREFIX=acme`.
#
# RESOURCE_PREFIX namespaces every physical resource name, so two installations can
# coexist in one account. See docs/ARCHITECTURE.md#resource-naming.

.PHONY: help preflight config lock stage images deploy deploy-all deploy-spas purge-orphans \
        outputs env cognito-user dev logs test-scan destroy clean check test smoke \
        golden golden-update envs env-info

.DEFAULT_GOAL := help

# ── WHICH ENVIRONMENT ─────────────────────────────────────────────────────────
# One checkout deploys any number of environments: each is a free-form name (dev, uat,
# env1, ...) with its settings in deploy.<name>.config and its build state in
# deployment/.state/<name>/, so two terminals can deploy two environments at once.
#
#   make deploy ENV=uat          this run only
#   export DEPLOY_ENV=uat        every run in this terminal (like AWS_PROFILE)
#   make deploy                  asks for the name first, listing the saved ones
#
# ENV is read only from the command line: a shell's own ENV variable (sh uses it for a
# startup file) must not silently pick an environment.
ifeq ($(origin ENV),command line)
DEPLOY_ENV := $(ENV)
endif

# Targets that read or write an environment's settings or deploy it.
ENV_TARGETS := preflight config deploy deploy-all stage images deploy-stack deploy-stack-run \
               deploy-spas destroy purge-orphans outputs env cognito-user logs test-scan smoke \
               check-settings settings-report

# Nothing named an environment, and no single-environment deploy.config is in use: ask,
# before deploy.<name>.config is included below. Only when a goal needs one - `make help`
# or `make test` never prompt.
ifeq ($(strip $(DEPLOY_ENV)),)
ifneq ($(origin CONFIG_FILE),command line)
ifeq ($(wildcard deploy.config),)
ifneq ($(filter $(ENV_TARGETS),$(MAKECMDGOALS)),)
DEPLOY_ENV := $(shell ./deployment/configure.py --choose-env)
ifeq ($(strip $(DEPLOY_ENV)),)
$(error No deployment environment chosen)
endif
endif
endif
endif
endif

# The name becomes a file name and a directory, so only letters, digits, '-' and '_'.
_ENV_CHARS := a b c d e f g h i j k l m n o p q r s t u v w x y z \
              A B C D E F G H I J K L M N O P Q R S T U V W X Y Z 0 1 2 3 4 5 6 7 8 9 - _
_strip_chars = $(if $(2),$(call _strip_chars,$(subst $(firstword $(2)),,$(1)),$(wordlist 2,$(words $(2)),$(2))),$(1))
ifneq ($(strip $(DEPLOY_ENV)),)
ifneq ($(words $(DEPLOY_ENV)),1)
$(error Environment name '$(DEPLOY_ENV)' has spaces: use letters, digits, '-' or '_')
endif
ifneq ($(call _strip_chars,$(DEPLOY_ENV),$(_ENV_CHARS)),)
$(error Environment name '$(DEPLOY_ENV)' is not allowed: use letters, digits, '-' or '_' (e.g. dev, uat, env1))
endif
CONFIG_FILE := deploy.$(strip $(DEPLOY_ENV)).config
DEPLOY_STATE_DIR := $(CURDIR)/deployment/.state/$(strip $(DEPLOY_ENV))
else
# A checkout that deploys one environment keeps a bare deploy.config, with its build
# state where it always was. A CONFIG_FILE given on the command line wins over both.
CONFIG_FILE := deploy.config
DEPLOY_STATE_DIR := $(CURDIR)/deployment
endif
export DEPLOY_ENV CONFIG_FILE DEPLOY_STATE_DIR

# Captured BEFORE the include, because that is the only point where the shell's value is
# still visible: an assignment in an included makefile overrides an environment variable.
# Restored immediately after the include - see "WHICH ACCOUNT" below.
ENV_AWS_PROFILE := $(AWS_PROFILE)

# Reports and validates the effective settings. A target rather than an inline call so
# it can be invoked as a sub-make, which re-parses deploy.config - see CONFIG_ENV below.
.PHONY: check-settings settings-report
# Adopt first, then report from a SUB-make. make parses deploy.config once, at startup,
# so a target that rewrites the file cannot see its own writes - $(INITIAL_ADMIN_EMAIL)
# would still be the value from before the adopt, and worse, would be passed to
# CloudFormation as an empty override. A fresh make re-parses.
check-settings:
	@./deployment/configure.py --adopt
	@$(MAKE) --no-print-directory settings-report

# Purely a report: it prints the effective settings (check-config.sh) and the template
# settings, and must never fail the recipe. check-config.sh exits 1 when a required
# setting is missing, so `|| true` keeps that from surfacing as a `make *** Error`
# banner in preflight (which is just showing the report and then gates on
# `configure.py --verify` separately). Without it, a first-time run — where the admin
# email is not set yet — looks like a crash instead of a prompt.
settings-report:
	@$(CONFIG_ENV) ./deployment/check-config.sh || true
	@echo ""
	@echo "🔍 Template settings (security, guardrails, capacity)..."
	@./deployment/configure.py --report

# deploy.config is plain `KEY = value` make syntax, so it is included rather than
# parsed. Missing file is not an error: `make deploy` creates it, and every setting
# has a default or is prompted for.
-include $(CONFIG_FILE)

RESOURCE_PREFIX ?= dbops
AWS_REGION      ?= us-east-1
STACK_NAME      ?= $(RESOURCE_PREFIX)
SOLUTION_NAME   ?= dbops-agent
VERSION         ?= v1.0.0
APP_NAME        ?= DBOps Agent (Beta)
INITIAL_ADMIN_EMAIL ?=
INITIAL_ADMIN_NAME  ?=
NOTIFICATION_EMAIL  ?=
# Optional. Must already be a verified SES identity for invitations to deliver;
# leaving it empty means user invitations are configured in the admin console later.
EMAIL_SENDER_ADDRESS ?=
EMAIL_SENDER_NAME    ?=

# WHICH ACCOUNT: from the shell, never from deploy.config.
#
# Credentials are ambient session context, not deployment configuration. The profile
# comes from `export AWS_PROFILE=<name>`, or from the default credential chain, exactly
# as it does for the aws CLI itself.
#
# Forced back to the shell's value UNCONDITIONALLY - including when that is empty. Two
# reasons it has to be unconditional rather than "only if the shell set one":
#   * deploy.config is included above, and any `AWS_PROFILE = x` line in it becomes a
#     make variable that overrides the environment. Files written before the setting was
#     removed still carry one.
#   * a stale saved profile silently redirecting a deploy is precisely what pointed a
#     `make destroy` at a different account than the operator's shell was on.
# Falling back to the default credential chain fails loudly in check-config.sh, which is
# the better failure.
#
# `make deploy AWS_PROFILE=x` still works: a command-line variable beats an assignment.
AWS_PROFILE := $(ENV_AWS_PROFILE)
ifneq ($(strip $(AWS_PROFILE)),)
export AWS_PROFILE
endif
export AWS_REGION

# Resolved lazily (=) rather than at parse time (:=) so targets that need no AWS
# call - help, clean - do not pay for one.
AWS_ACCOUNT_ID = $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
ARTIFACTS_BUCKET ?= $(RESOURCE_PREFIX)-artifacts-$(AWS_ACCOUNT_ID)-$(AWS_REGION)

CONTAINER_CMD := $(shell command -v docker 2>/dev/null || command -v finch 2>/dev/null || command -v podman 2>/dev/null || command -v nerdctl 2>/dev/null)

# The settings check reads its values from the environment, so there is one source of
# truth rather than a second parser for deploy.config. Passed explicitly rather than
# exported globally, so it is visible which settings it consumes.
# Run as a sub-make wherever the settings may have just been rewritten. make parses
# deploy.config once, at startup, via the -include above, so a target that writes the
# file cannot then re-check it in the same process - $(INITIAL_ADMIN_EMAIL) would still
# hold the value from before the write. A fresh make re-parses it.
CONFIG_ENV = CONFIG_FILE='$(CONFIG_FILE)' \
             RESOURCE_PREFIX='$(RESOURCE_PREFIX)' \
             STACK_NAME='$(STACK_NAME)' \
             INITIAL_ADMIN_EMAIL='$(INITIAL_ADMIN_EMAIL)' \
             INITIAL_ADMIN_NAME='$(INITIAL_ADMIN_NAME)' \
             NOTIFICATION_EMAIL='$(NOTIFICATION_EMAIL)' \
             EMAIL_SENDER_ADDRESS='$(EMAIL_SENDER_ADDRESS)'

help:
	@echo ""
	@echo "DB Ops Agent"
	@echo "============"
	@echo ""
	@echo "  Deploy (ENV=<name> picks an environment; without it, deploy asks)"
	@echo "    make preflight     Check prerequisites"
	@echo "    make deploy        Interactive deploy (prompts, then saves deploy.<name>.config)"
	@echo "    make deploy-all    Non-interactive deploy (uses the saved settings)"
	@echo "    make envs          List saved environments: account, region, stack, VPC range"
	@echo "    make cognito-user  Create a console user"
	@echo ""
	@echo "  Individual steps (deploy runs all four in order)"
	@echo "    make stage         Build + upload templates and Lambda artifacts"
	@echo "    make images        Build + push the backend and batch images"
	@echo "    make deploy-stack  Deploy infrastructure/master.yaml"
	@echo "    make deploy-spas   Build + publish the operator app and admin console"
	@echo ""
	@echo "  Verify"
	@echo "    make check         Static checks (no AWS, no containers)"
	@echo "    make test          Unit tests + checker guard tests + golden harness"
	@echo "    make golden        Golden harness, Tier A (built apps, fake backend)"
	@echo "    make golden-update Rewrite the golden text snapshots"
	@echo "    make smoke         Smoke-test the deployed stack"
	@echo ""
	@echo "  Operate"
	@echo "    make outputs       Show stack outputs"
	@echo "    make env           Write .env files for local development"
	@echo "    make logs          Tail the agent runtime logs"
	@echo "    make test-scan INSTANCE_ID=<id>  Trigger a deep scan of one database"
	@echo "    make dev           Run the backend locally on port 8084"
	@echo ""
	@echo "  Teardown"
	@echo "    make destroy       Delete the stack and its buckets (destructive)"
	@echo "    make clean         Remove local build artifacts only"
	@echo ""
	@echo "  Current settings ($(CONFIG_FILE)$(if $(strip $(DEPLOY_ENV)),, - no environment chosen)):"
	@echo "    RESOURCE_PREFIX = $(RESOURCE_PREFIX)"
	@echo "    AWS_REGION      = $(AWS_REGION)"
	@echo "    STACK_NAME      = $(STACK_NAME)"
	@echo ""

# Every saved environment, and how to pick one. Reads the files only; no AWS call.
envs:
	@./deployment/configure.py --envs

# Where this run's settings and build state live. For scripts and tests.
env-info:
	@echo "DEPLOY_ENV=$(DEPLOY_ENV)"
	@echo "CONFIG_FILE=$(CONFIG_FILE)"
	@echo "DEPLOY_STATE_DIR=$(DEPLOY_STATE_DIR)"

# ──────────────────────────────────────────────────────────────────────────────
# Preflight
# ──────────────────────────────────────────────────────────────────────────────
preflight:
	@echo "🔍 Checking prerequisites..."
	@PASS=true; \
	printf "   AWS CLI.............. "; \
	if command -v aws >/dev/null 2>&1; then echo "✅ $$(aws --version 2>&1 | head -1)"; \
	else echo "❌ not found"; PASS=false; fi; \
	printf "   AWS credentials...... "; \
	if aws sts get-caller-identity >/dev/null 2>&1; then \
		echo "✅ account $$(aws sts get-caller-identity --query Account --output text)$${AWS_PROFILE:+ (profile $$AWS_PROFILE)}"; \
	else \
		if [ -n "$(strip $(AWS_PROFILE))" ]; then \
			echo "❌ profile '$(strip $(AWS_PROFILE))' has no valid credentials"; \
		else \
			echo "❌ no credentials — run 'aws configure', or set AWS_PROFILE=<name>"; \
		fi; \
		PASS=false; \
	fi; \
	./deployment/check-container.sh || PASS=false; \
	printf "   Python 3............. "; \
	if command -v python3 >/dev/null 2>&1; then echo "✅ $$(python3 --version)"; \
	else echo "❌ not found"; PASS=false; fi; \
	printf "   Node.js.............. "; \
	if command -v node >/dev/null 2>&1; then echo "✅ $$(node --version)"; \
	else echo "❌ not found"; PASS=false; fi; \
	printf "   zip / unzip.......... "; \
	if command -v zip >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1; then echo "✅"; \
	else echo "❌ not found"; PASS=false; fi; \
	echo ""; \
	echo "🔍 Checking deployment settings..."; \
	./deployment/configure.py --adopt; \
	if ./deployment/configure.py --verify >/dev/null 2>&1; then \
		$(MAKE) --no-print-directory settings-report; \
	elif [ -t 0 ]; then \
		echo "   Deployment settings need a few values. Let's set them now."; \
		$(MAKE) --no-print-directory config || exit 1; \
		echo ""; \
		echo "🔍 Deployment settings..."; \
		$(MAKE) --no-print-directory settings-report; \
		./deployment/configure.py --verify >/dev/null 2>&1 || PASS=false; \
	else \
		$(MAKE) --no-print-directory settings-report; \
		PASS=false; \
	fi; \
	echo ""; \
	if [ "$$PASS" != "true" ]; then echo "❌ Fix the issues above and re-run."; exit 1; fi; \
	if [ -t 0 ]; then \
		if ./deployment/configure.py --confirm "Change any of the settings above?"; then \
			$(MAKE) --no-print-directory config || exit 1; \
			echo ""; \
			$(MAKE) --no-print-directory check-settings; \
			./deployment/configure.py --verify >/dev/null 2>&1 || exit 1; \
		fi; \
	fi; \
	echo ""; \
	echo "✅ Ready to deploy — run 'make deploy'."

# Walks every setting one at a time, showing the current value to accept or change.
# Seeds deploy.config from the committed deploy.config.example on first run, so the
# file keeps its explanatory comments instead of becoming a list of bare keys.
# Gitignored: it holds an account-specific bucket name and the admin's email.
config:
	@./deployment/configure.py

# Regenerate the pinned, hashed dependency locks (backend/ and batch/ requirements.txt)
# from the requirements.in sources after editing them. The images install with
# --require-hashes, so the lock must be refreshed whenever a dependency changes.
lock:
	@./deployment/lock-requirements.sh

# ──────────────────────────────────────────────────────────────────────────────
# Deploy steps
# ──────────────────────────────────────────────────────────────────────────────
stage:
	@./deployment/configure.py --check-account
	@AWS_REGION=$(AWS_REGION) ./deployment/build-s3-dist.sh \
		"$(ARTIFACTS_BUCKET)" "$(SOLUTION_NAME)" "$(VERSION)" --upload

images:
	@./deployment/configure.py --check-account
	@AWS_REGION=$(AWS_REGION) ./deployment/build-images.sh "$(RESOURCE_PREFIX)" "$(AWS_REGION)"

# Adopt any missing settings from the deployed stack, then hand off to a SUB-make so
# the freshly written file is actually parsed. Without the split, a just-adopted
# InitialAdminEmail is still empty in this process and would be passed as an empty
# override - blanking on the deployed stack the very value it was read from.
.PHONY: deploy-stack deploy-stack-run
deploy-stack:
	@./deployment/configure.py --adopt --quiet
	@$(MAKE) --no-print-directory deploy-stack-run

deploy-stack-run:
	@./deployment/configure.py --check-account
	@if [ ! -f "$(DEPLOY_STATE_DIR)/.artifacts-env" ]; then \
		echo "❌ $(DEPLOY_STATE_DIR)/.artifacts-env missing — run 'make stage' first."; exit 1; fi
	@if [ ! -f "$(DEPLOY_STATE_DIR)/.images-env" ]; then \
		echo "❌ $(DEPLOY_STATE_DIR)/.images-env missing — run 'make images' first."; exit 1; fi
	@./deployment/configure.py --verify
	@$(CONFIG_ENV) ./deployment/check-config.sh --quiet
	@# The AWS managed keys the stack encrypts with (DynamoDB tables, the SNS topic) do not
	@# exist in an account that has never used them; the first resource to need one creates
	@# it, and resources created in the same instant can be refused with "KMS key ... does
	@# not exist" before it propagates. DescribeKey on the predefined alias creates the key
	@# up front (documented KMS behaviour), so it exists long before CloudFormation asks.
	@# Best effort: storage.yaml also orders the tables, so a principal without
	@# kms:DescribeKey still deploys.
	@for a in alias/aws/dynamodb alias/aws/sns; do \
		aws kms describe-key --key-id "$$a" --region $(AWS_REGION) --query KeyMetadata.KeyId --output text >/dev/null 2>&1 || true; \
	done
	@echo "🔎 Checking for leftovers from a previous failed deploy"
	@./deployment/check-orphans.sh "$(STACK_NAME)" "$(RESOURCE_PREFIX)" "$(AWS_REGION)"
	@set -a; . "$(DEPLOY_STATE_DIR)/.artifacts-env"; . "$(DEPLOY_STATE_DIR)/.images-env"; set +a; \
	echo ""; \
	echo "🚀 Deploying $(STACK_NAME) to $(AWS_REGION)"; \
	echo "   templates: $$TEMPLATE_BASE_URL"; \
	echo "   artifacts: $$ARTIFACTS_VERSION"; \
	FRONTEND_WAF_ARN=""; ADMIN_WAF_ARN=""; \
	if [ "$(AWS_REGION)" != "us-east-1" ] && [ "$(ENABLE_WAF)" != "false" ]; then \
		EDGE_STACK="$(STACK_NAME)-edge-$(AWS_REGION)"; \
		echo "   🛡️  CloudFront WAF: deploying edge stack $$EDGE_STACK to us-east-1 (enforced at the edge)"; \
		aws cloudformation deploy \
			--region us-east-1 \
			--template-file infrastructure/edge-waf.yaml \
			--stack-name "$$EDGE_STACK" \
			--parameter-overrides ResourcePrefix=$(RESOURCE_PREFIX) AppRegion=$(AWS_REGION) \
			--no-fail-on-empty-changeset || { echo "❌ CloudFront WAF edge stack failed; aborting before the main stack."; exit 1; }; \
		FRONTEND_WAF_ARN=$$(aws cloudformation describe-stacks --region us-east-1 --stack-name "$$EDGE_STACK" \
			--query "Stacks[0].Outputs[?OutputKey=='FrontendWebAclArn'].OutputValue" --output text); \
		ADMIN_WAF_ARN=$$(aws cloudformation describe-stacks --region us-east-1 --stack-name "$$EDGE_STACK" \
			--query "Stacks[0].Outputs[?OutputKey=='AdminWebAclArn'].OutputValue" --output text); \
	fi; \
	aws cloudformation deploy \
		--region $(AWS_REGION) \
		--template-file infrastructure/master.yaml \
		--stack-name $(STACK_NAME) \
		--capabilities CAPABILITY_NAMED_IAM \
		--parameter-overrides \
			ResourcePrefix=$(RESOURCE_PREFIX) \
			FrontendWebAclArn=$$FRONTEND_WAF_ARN \
			AdminWebAclArn=$$ADMIN_WAF_ARN \
			BackendImageUri=$$BACKEND_IMAGE_URI \
			BatchImageUri=$$BATCH_IMAGE_URI \
			TemplateBaseUrl=$$TEMPLATE_BASE_URL \
			ArtifactsBucket=$$ARTIFACTS_BUCKET \
			ArtifactsVersion=$$ARTIFACTS_VERSION \
			InitialAdminEmail='$(INITIAL_ADMIN_EMAIL)' \
			InitialAdminName='$(INITIAL_ADMIN_NAME)' \
			NotificationEmail='$(NOTIFICATION_EMAIL)' \
			EmailSenderAddress='$(EMAIL_SENDER_ADDRESS)' \
			EmailSenderName='$(EMAIL_SENDER_NAME)' \
			AppName='$(APP_NAME)' \
			$(if $(strip $(ENABLE_WAF)),EnableWAF=$(ENABLE_WAF),) \
			$(if $(strip $(ENABLE_CLOUDTRAIL)),EnableCloudTrail=$(ENABLE_CLOUDTRAIL),) \
			$(if $(strip $(ENABLE_COGNITO_THREAT_PROTECTION)),EnableCognitoThreatProtection=$(ENABLE_COGNITO_THREAT_PROTECTION),) \
			$(if $(strip $(ENABLE_GUARDRAIL)),EnableGuardrail=$(ENABLE_GUARDRAIL),) \
			$(if $(strip $(GUARDRAIL_CONTENT_STRENGTH)),GuardrailContentStrength=$(GUARDRAIL_CONTENT_STRENGTH),) \
			$(if $(strip $(GUARDRAIL_PROMPT_ATTACK_STRENGTH)),GuardrailPromptAttackStrength=$(GUARDRAIL_PROMPT_ATTACK_STRENGTH),) \
			$(if $(strip $(GUARDRAIL_PII_ACTION)),GuardrailPiiAction=$(GUARDRAIL_PII_ACTION),) \
			$(if $(strip $(GUARDRAIL_THROTTLE_ALARM_THRESHOLD)),GuardrailThrottleAlarmThreshold=$(GUARDRAIL_THROTTLE_ALARM_THRESHOLD),) \
			$(if $(strip $(GUARDRAIL_INTERVENTION_ALARM_THRESHOLD)),GuardrailInterventionAlarmThreshold=$(GUARDRAIL_INTERVENTION_ALARM_THRESHOLD),) \
			$(if $(strip $(ENABLE_WEB_SEARCH)),EnableWebSearch=$(ENABLE_WEB_SEARCH),) \
			$(if $(strip $(AGENT_MODEL_ID)),AgentModelId=$(AGENT_MODEL_ID),) \
			$(if $(strip $(TIER1_RETENTION_DAYS)),Tier1RetentionDays=$(TIER1_RETENTION_DAYS),) \
			$(if $(strip $(LOG_RETENTION_DAYS)),LogRetentionDays=$(LOG_RETENTION_DAYS),) \
			$(if $(strip $(REFRESH_TOKEN_VALIDITY_DAYS)),RefreshTokenValidityDays=$(REFRESH_TOKEN_VALIDITY_DAYS),) \
			$(if $(strip $(BUDGET_LIMIT_AMOUNT)),BudgetLimitAmount=$(BUDGET_LIMIT_AMOUNT),) \
			$(if $(strip $(JWT_SIGNATURE_VERIFICATION)),JwtSignatureVerification=$(JWT_SIGNATURE_VERIFICATION),) \
			$(if $(strip $(DB_PORT_RANGE)),DbPortRange=$(DB_PORT_RANGE),) \
			$(if $(strip $(SAME_REGION_CONNECTIVITY)),SameRegionConnectivity=$(SAME_REGION_CONNECTIVITY),) \
			$(if $(strip $(AUDIT_VAULT)),AuditVault=$(AUDIT_VAULT),) \
			$(if $(strip $(AUDIT_RETENTION_DAYS)),AuditRetentionDays=$(AUDIT_RETENTION_DAYS),) \
			$(if $(strip $(VPC_CIDR)),VpcCidr=$(VPC_CIDR),) \
			$(if $(strip $(PUBLIC_SUBNET1_CIDR)),PublicSubnet1Cidr=$(PUBLIC_SUBNET1_CIDR),) \
			$(if $(strip $(PUBLIC_SUBNET2_CIDR)),PublicSubnet2Cidr=$(PUBLIC_SUBNET2_CIDR),) \
			$(if $(strip $(PRIVATE_SUBNET1_CIDR)),PrivateSubnet1Cidr=$(PRIVATE_SUBNET1_CIDR),) \
			$(if $(strip $(PRIVATE_SUBNET2_CIDR)),PrivateSubnet2Cidr=$(PRIVATE_SUBNET2_CIDR),) \
		--no-fail-on-empty-changeset; \
	rc=$$?; \
	if [ $$rc -eq 0 ] && [ "$(AWS_REGION)" != "us-east-1" ] && [ "$(ENABLE_WAF)" = "false" ]; then \
		EDGE_STACK="$(STACK_NAME)-edge-$(AWS_REGION)"; \
		if aws cloudformation describe-stacks --region us-east-1 --stack-name "$$EDGE_STACK" >/dev/null 2>&1; then \
			echo "   🛡️  WAF is off: removing the now-unused CloudFront WAF edge stack $$EDGE_STACK (us-east-1)"; \
			aws cloudformation delete-stack --region us-east-1 --stack-name "$$EDGE_STACK"; \
		fi; \
	fi; \
	exit $$rc
	@echo "✅ Stack deployed"

deploy-spas:
	@./deployment/configure.py --check-account
	@APP_NAME='$(APP_NAME)' AWS_REGION=$(AWS_REGION) \
		./deployment/deploy-spas.sh "$(STACK_NAME)" "$(AWS_REGION)"

# `make deploy` prompts only when there is no saved config, so a redeploy is a
# single command with no questions.
# Prompts when the settings are missing OR unusable, not merely when the file is
# absent. An existing deploy.config with an empty admin email would otherwise sail
# past this and fail 30 minutes later - or worse, succeed and mail the temporary
# password to whoever was in an inherited file.
deploy:
	@./deployment/configure.py --check-account
	@./deployment/configure.py --adopt
	@if ! ./deployment/configure.py --verify 2>/dev/null; then \
		if [ -t 0 ]; then \
			./deployment/configure.py --verify || true; \
			echo "   Let's fill them in."; \
			$(MAKE) --no-print-directory config; \
		else \
			./deployment/configure.py --verify; exit 1; \
		fi; \
	fi
	@$(MAKE) --no-print-directory deploy-all

# Order matters: templates and Lambda archives must exist in S3 before the stack
# references them, images before the stack creates the runtime and job definition,
# and the SPAs last because config.js is built from stack outputs.
deploy-all: stage images deploy-stack deploy-spas
	@echo ""
	@echo "🎉 Deployment complete."
	@echo ""
	@$(MAKE) --no-print-directory outputs
	@echo "Sign in with the admin email you supplied — Cognito emailed a temporary password."
	@echo "Add more users with: make cognito-user"
	@echo ""

# ──────────────────────────────────────────────────────────────────────────────
# Operate
# ──────────────────────────────────────────────────────────────────────────────
outputs:
	@aws cloudformation describe-stacks --region $(AWS_REGION) --stack-name $(STACK_NAME) \
		--query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table

# Local development only: writes backend/.env, frontend/.env and admin-console/.env
# from the deployed stack. A deployed backend gets these from its task definition and
# the deployed web apps read config.js, so this is not part of the deploy path.
env:
	@python3 deployment/generate-env.py --stack-name $(STACK_NAME) --region $(AWS_REGION)

cognito-user:
	@cd infrastructure && AWS_REGION=$(AWS_REGION) STACK_NAME=$(STACK_NAME) ./create-user.sh

dev:
	@$(MAKE) -C backend dev

logs:
	@LOG_GROUP=$$(aws logs describe-log-groups --region $(AWS_REGION) \
		--log-group-name-prefix "/aws/bedrock-agentcore/runtimes/" \
		--query 'logGroups[0].logGroupName' --output text) && \
	if [ -z "$$LOG_GROUP" ] || [ "$$LOG_GROUP" = "None" ]; then \
		echo "No agent runtime log group found yet."; exit 0; fi && \
	echo "📜 $$LOG_GROUP (last 15 minutes)" && \
	aws logs filter-log-events --region $(AWS_REGION) --log-group-name "$$LOG_GROUP" \
		--start-time $$(python3 -c 'import time; print(int((time.time()-900)*1000))') \
		--query 'events[*].[timestamp,message]' --output text | sort -n

test-scan:
	@if [ -z "$(INSTANCE_ID)" ]; then \
		echo "❌ INSTANCE_ID is required: the scan trigger scans ONE database."; \
		echo "   Usage: make test-scan INSTANCE_ID=<registry instance id>"; \
		echo "   (an empty payload scanned nothing and reported success)"; \
		exit 1; \
	fi
	@aws lambda invoke --region $(AWS_REGION) \
		--function-name $(RESOURCE_PREFIX)-scan-trigger \
		--payload '{"instance_id":"$(INSTANCE_ID)"}' /tmp/$(RESOURCE_PREFIX)-scan.json >/dev/null && \
		cat /tmp/$(RESOURCE_PREFIX)-scan.json && echo ""

# ──────────────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────────────
# Static checks only: no AWS calls, no container runtime, nothing mutated. Safe to
# run at any time and cheap enough to run often.
check:
	@echo "── resource naming ──"
	@python3 deployment/check-resource-names.py
	@echo ""
	@echo "── templates ──"
	@python3 deployment/check-templates.py
	@echo ""
	@echo "── deploy settings reach CloudFormation ──"
	@python3 deployment/check-config-defaults.py
	@echo ""
	@echo "── python syntax ──"
	@python3 -m compileall -q backend batch deployment && echo "  ok   compiles"
	@if python3 -c 'import pyflakes' 2>/dev/null; then \
		python3 -m pyflakes backend batch deployment && echo "  ok   no unused/undefined names"; \
	else \
		echo "  --   pyflakes not installed (pip install pyflakes) - skipped"; \
	fi
	@echo ""
	@echo "── golden harness syntax ──"
	@if command -v node >/dev/null 2>&1; then \
		rc=0; for f in $$(find tests/golden -name '*.mjs'); do node --check "$$f" || rc=1; done; \
		[ $$rc -eq 0 ] && echo "  ok   tests/golden parses" || exit 1; \
	else \
		echo "  --   node not installed - skipped"; \
	fi
	@echo ""
	@echo "── shell syntax ──"
	@rc=0; for s in deployment/*.sh infrastructure/*.sh backend/scripts/*.sh tests/*.sh \
		tests/e2e/*.sh frontend/docker-entrypoint.sh admin-console/docker-entrypoint.sh; do \
		[ -f "$$s" ] || continue; \
		bash -n "$$s" || rc=1; \
	done; \
	[ $$rc -eq 0 ] && echo "  ok   all shell scripts parse" || exit 1
	@echo ""
	@echo "── executable bits ──"
# Anything this Makefile runs as ./path has to be executable, or the recipe dies with
# "Permission denied" - which happened, and only showed up when that code path was
# exercised. A missing mode bit is also easy to lose when a file is copied or patched,
# so it is checked here rather than trusted.
# Dot-prefixed files are excluded: .artifacts-env and .images-env are sourced with `.`,
# not executed, so they are matched by the same pattern but must not be executable.
	@rc=0; for s in $$(grep -oE '\./deployment/[A-Za-z0-9_.-]+' Makefile | grep -v '/\.' | sort -u); do \
		[ -f "$$s" ] || continue; \
		if [ ! -x "$$s" ]; then echo "  ❌ $$s is run as '$$s' but is not executable (chmod +x)"; rc=1; fi; \
	done; \
	[ $$rc -eq 0 ] && echo "  ok   everything invoked directly is executable" || exit 1

# Unit tests and the guard tests. Separate from `check` because test_check_guards.sh
# deliberately mutates tracked files to prove the checkers fail, restoring them on
# exit - correct, but not something a plain `check` should do.
test:
	@echo "── resource_names unit tests ──"
	@python3 tests/test_resource_names.py
	@echo ""
	@echo "── checker guard tests ──"
	@bash tests/test_check_guards.sh
	@echo ""
	@echo "── deployment settings guard tests ──"
	@bash tests/test_check_config.sh
	@echo ""
	@echo "── destroy confirmation tests ──"
	@bash tests/test_destroy_confirm.sh
	@echo ""
	@echo "── CIDR wiring tests ──"
	@python3 tests/test_cidr_wiring.py
	@echo ""
	@echo "── shared database-list controls tests ──"
	@if command -v node >/dev/null 2>&1; then node tests/test_db_list_controls.mjs; \
	else echo "  --   node not installed - skipped"; fi
	@echo ""
	@echo "── accounts & regions UI tests ──"
	@if command -v node >/dev/null 2>&1; then node tests/test_regions_per_account_ui.mjs; \
	else echo "  --   node not installed - skipped"; fi
	@echo ""
	@echo "── agent trace model line tests ──"
	@if command -v node >/dev/null 2>&1; then node tests/test_model_trace_label.mjs; \
	else echo "  --   node not installed - skipped"; fi
	@echo ""
	@echo "── model resolution tests ──"
	@python3 tests/test_model_resolution.py
	@echo ""
	@echo "── disabled default + resolved-model trace tests ──"
	@python3 tests/test_disabled_default_and_trace.py
	@echo ""
	@echo "── refused chat turn is visible tests ──"
	@python3 tests/test_refusal_visible.py
	@echo ""
	@echo "── bulk add-to-scope selection tests ──"
	@python3 tests/test_bulk_scope.py
	@echo ""
	@echo "── database TLS tests ──"
	@python3 tests/test_db_tls.py
	@echo ""
	@echo "── scan read-only session tests ──"
	@python3 tests/test_scan_readonly.py
	@echo ""
	@echo "── threat model guard tests ──"
	@python3 tests/test_threat_model_guards.py
	@echo ""
	@echo "── guardrail wiring tests ──"
	@python3 tests/test_guardrail_wiring.py
	@echo ""
	@echo "── API stage ordering tests ──"
	@python3 tests/test_api_stage_ordering.py
	@echo ""
	@echo "── cross-account trust tests ──"
	@python3 tests/test_cross_account_trust.py
	@echo ""
	@echo "── cross-account space binding tests ──"
	@python3 tests/test_external_id_binding.py
	@echo ""
	@echo "── agent-role-policy authz tests ──"
	@python3 tests/test_agent_role_policy_authz.py
	@echo ""
	@echo "── token lifecycle tests ──"
	@python3 tests/test_token_lifecycle.py
	@echo ""
	@echo "── member-account role policy tests ──"
	@python3 tests/test_secondary_role_policy.py
	@echo ""
	@echo "── metrics-store (DynamoDB + CloudWatch) role split tests ──"
	@python3 tests/test_metrics_store_roles.py
	@echo ""
	@echo "── database host precedence tests ──"
	@python3 tests/test_host_precedence.py
	@echo ""
	@echo "── XID wraparound denominator tests ──"
	@python3 tests/test_xid_denominator.py
	@echo ""
	@echo "── make test-scan requires INSTANCE_ID ──"
	@python3 tests/test_make_test_scan.py
	@echo ""
	@echo "── theme token tests ──"
	@python3 tests/test_theme_tokens.py
	@echo ""
	@echo "── peering completion tests ──"
	@python3 tests/test_peering_completion.py
	@echo ""
	@echo "── Lattice path tests ──"
	@python3 tests/test_lattice_path.py
	@echo ""
	@echo "── orphan-check name tests ──"
	@python3 tests/test_orphan_names.py
	@echo ""
	@echo "── non-interactive settings tests ──"
	@python3 tests/test_noninteractive.py
	@echo ""
	@echo "── deployment environments tests ──"
	@python3 tests/test_deploy_envs.py
	@echo ""
	@echo "── KMS managed keys on a first deploy ──"
	@python3 tests/test_kms_first_use.py
	@echo ""
	@echo "── audit settings (configure) tests ──"
	@python3 tests/test_configure_audit.py
	@echo ""
	@echo "── documentation accuracy tests ──"
	@python3 tests/test_docs_accuracy.py
	@echo ""
	@echo "── golden harness Tier A (all scenarios, ~50 s) ──"
	@node tests/golden/run.mjs

	@echo ""
	@echo "── supply-chain build guard tests ──"
	@python3 tests/test_supply_chain_build.py

	@echo ""
	@echo "── report-context literal redaction tests ──"
	@python3 tests/test_report_literal_redaction.py
	@echo ""
	@echo "── batch report fence breakout tests ──"
	@python3 tests/test_report_fence_breakout.py

	@echo ""
	@echo "── scan task egress isolation tests ──"
	@python3 tests/test_scan_egress_isolation.py
	@echo ""
	@echo "── in-app JWT enforcement tests ──"
	@python3 tests/test_jwt_enforcement.py
	@echo ""
	@echo "── operator app single-role tests ──"
	@python3 tests/test_operator_single_role.py
	@echo ""
	@echo "── operator-only capability gate tests ──"
	@python3 tests/test_operator_capability_gate.py
	@echo ""
	@echo "── no raw exception text to the client tests ──"
	@python3 tests/test_no_raw_exception_detail.py
	@echo ""
	@echo "── waiting-queries active-only tests ──"
	@python3 tests/test_waiting_queries_active_only.py
	@echo ""
	@echo "── XID whole-instance measurement tests ──"
	@python3 tests/test_xid_whole_instance.py
	@echo ""
	@echo "── deep-scan DB size tests ──"
	@python3 tests/test_deep_scan_db_size.py
	@echo ""
	@echo "── report/investigation context stays inside tests ──"
	@python3 tests/test_report_context_no_live.py
	@echo ""
	@echo "── prices live in one place ──"
	@python3 tests/test_no_price_figures.py
	@echo ""
	@echo "── Region-gated features (WAF, web search) tests ──"
	@python3 tests/test_region_gated_features.py
	@echo ""
	@echo "── task role ENI guard tests ──"
	@python3 tests/test_task_role_eni_guard.py
	@echo ""
	@echo "── task role network policy tests ──"
	@python3 tests/test_task_role_network_policy.py
	@echo ""
	@echo "── cross-Region host route planner tests ──"
	@python3 tests/test_host_routes.py
	@echo ""
	@echo "── Tier 1 collector role policy tests ──"
	@python3 tests/test_tier1_role_policy.py
	@echo ""
	@echo "── Tier 2 reader scan tests ──"
	@python3 tests/test_reader_scan.py
	@echo ""
	@echo "── shared front-end component tests ──"
	@python3 tests/test_shared_components.py
	@echo ""
	@echo "── live member panels refresh tests ──"
	@node tests/test_live_poll.mjs
	@echo ""
	@echo "── report/investigation context follow-up tests ──"
	@node tests/test_context_followups.mjs
	@echo ""
	@echo "── report-mode send-to-live-agent tests ──"
	@node tests/test_live_handoff.mjs
	@echo ""
	@echo "── investigation run lifecycle (cancel / finalize) tests ──"
	@node tests/test_investigation_lifecycle.mjs
	@echo ""
	@echo "── backend guard + unit tests ──"
	@# Globbed rather than listed: these pin security invariants (space scoping, secret
	@# ownership, guardrail fail-closed, MFA preservation, SSRF allowlist, trace
	@# redaction), so a new one must not be able to sit in the tree unrun just because
	@# nobody added a line here.
	@fail=0; for t in backend/tests/test_*.py; do \
		printf '  %-44s ' "$$(basename $$t)"; \
		if out=$$(python3 "$$t" 2>&1); then \
			echo "$$out" | tail -n 1; \
		else \
			echo "FAILED"; echo "$$out" | tail -n 20; fail=1; \
		fi; \
	done; exit $$fail

# Golden harness (tests/golden/README.md). It walks the product's flows in the built
# apps against a stateful fake backend and checks docs/PRODUCT-RULES.md at every step;
# no AWS, no network beyond localhost.
golden:
	@node tests/golden/run.mjs $(SCENARIOS)

golden-update:
	@GOLDEN_UPDATE=1 node tests/golden/run.mjs $(SCENARIOS)


# End-to-end smoke tests against the deployed stack.
smoke:
	@URL=$$(aws cloudformation describe-stacks --region $(AWS_REGION) --stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text 2>/dev/null); \
	if [ -z "$$URL" ] || [ "$$URL" = "None" ]; then \
		echo "❌ Could not resolve DashboardUrl from stack '$(STACK_NAME)' in $(AWS_REGION)."; \
		echo "   Deploy first (make deploy), or pass a URL: bash tests/e2e/run.sh <url>"; \
		exit 1; \
	fi; \
	bash tests/e2e/run.sh "$$URL"

# ──────────────────────────────────────────────────────────────────────────────
# Teardown
# ──────────────────────────────────────────────────────────────────────────────
# S3 buckets must be emptied before CloudFormation can delete them, and the
# Remove what a failed deploy left behind. Retained resources survive a rollback, and
# CloudFormation then refuses to recreate them - reporting only "Validation failed with
# N error(s)" and naming nothing. Run this, then deploy again.
# The listing, the account name and the typed confirmation all live inside the script -
# see deployment/destroy/confirm.sh for why they are not here. This target is only a
# convenient way to reach it.
purge-orphans:
	@./deployment/configure.py --check-account
	@./deployment/destroy/purge-orphans.sh "$(STACK_NAME)" "$(RESOURCE_PREFIX)" "$(AWS_REGION)"

# CloudTrail bucket is deliberately RETAIN so the audit trail survives teardown.
destroy:
	@# Name the ACCOUNT, not just the stack. Every account uses the same stack name, so
	@# "delete stack dbops" is not enough information to consent to - a saved profile
	@# once pointed this at a different account than the shell, and the prompt gave no
	@# clue. Refuse outright if the stack is not in the account being targeted.
	@# Deletion protection on the metrics store makes CloudFormation fail PART WAY
	@# through the delete, after other resources are already gone, with an error that
	@# names a cluster rather than the flag. Check it up front and say what to do.
	@# (Comments live up here: inside the one backslash-continued shell command below,
	@# an '@#' line would reach the shell as a command and end the recipe.)
	@# An environment pinned to another account refuses before anything is described.
	@./deployment/configure.py --check-account
	@ACCT=$$(aws sts get-caller-identity --query Account --output text 2>/dev/null); \
	if [ -z "$$ACCT" ]; then \
		echo "❌ No usable AWS credentials — refusing to delete anything."; exit 1; fi; \
	if ! aws cloudformation describe-stacks --region $(AWS_REGION) --stack-name $(STACK_NAME) >/dev/null 2>&1; then \
		echo "❌ No stack named '$(STACK_NAME)' in account $$ACCT ($(AWS_REGION))."; \
		echo "   Nothing to delete here. If you meant a different account, select its"; \
		echo "   profile and re-run — the shell's AWS_PROFILE decides the target."; \
		exit 1; fi; \
	echo ""; \
	echo "╔══════════════════════════════════════════════════════════════════════╗"; \
	echo "║  DESTRUCTIVE OPERATION                                               ║"; \
	echo "╚══════════════════════════════════════════════════════════════════════╝"; \
	echo ""; \
	echo "   Deleting the whole deployment:"; \
	echo ""; \
	echo "       account  $$ACCT$${AWS_PROFILE:+  (profile $$AWS_PROFILE)}"; \
	echo "       region   $(AWS_REGION)"; \
	echo "       stack    $(STACK_NAME)"; \
	echo ""; \
	echo "   Gone for good: the Tier 1 metrics table (the rolling 5-minute detail), both"; \
	echo "   web apps, and the contents of the reports and knowledge buckets. CloudWatch"; \
	echo "   metrics age out on their own (15 months); they cannot be deleted."; \
	echo ""; \
	echo "   Kept (DeletionPolicy: Retain) so a redeploy resumes where you left off:"; \
	echo "   the DynamoDB registries - spaces, onboarded databases, investigations, the"; \
	echo "   deep-scan history and the audit trail - plus the log and CloudTrail buckets."; \
	echo "   'make purge-orphans'"; \
	echo "   removes those afterwards if you want the account truly empty. Also kept:"; \
	echo "   the audit vault bucket (locked records cannot be deleted before they"; \
	echo "   expire); it is deleted here only if it is already empty."; \
	echo ""; \
	echo "   This cannot be undone. Nothing has been deleted yet."; \
	echo ""; \
	printf "   To proceed, type '%s@%s' exactly (anything else aborts): " "$(STACK_NAME)" "$$ACCT"; \
	IFS= read -r CONFIRM; \
	if [ "$$CONFIRM" != "$(STACK_NAME)@$$ACCT" ]; then \
		echo ""; \
		echo "   That does not match '$(STACK_NAME)@$$ACCT' — aborted. Nothing was deleted."; \
		exit 1; fi; \
	export DESTROY_CONFIRMED="$(STACK_NAME)@$$ACCT"; \
	echo ""; \
	AUDIT_B=$$(aws cloudformation describe-stacks --region $(AWS_REGION) --stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='AuditBucketName'].OutputValue" --output text 2>/dev/null); \
	: 'The two emptying loops below name only the web, reports and knowledge buckets.' ; \
	: 'Never the audit vault: its records are under Object Lock COMPLIANCE, so nothing' ; \
	: 'can empty it, and it is deleted after the stack only if it is already empty.' ; \
	for KEY in FrontendBucketName AdminBucketName; do \
		B=$$(aws cloudformation describe-stacks --region $(AWS_REGION) --stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='$$KEY'].OutputValue" --output text 2>/dev/null); \
		if [ -n "$$B" ] && [ "$$B" != "None" ]; then \
			echo "   emptying s3://$$B"; \
			aws s3 rm "s3://$$B" --recursive --region $(AWS_REGION) --only-show-errors || true; \
		fi; \
	done; \
	for SUFFIX in reports knowledge; do \
		B="$(RESOURCE_PREFIX)-$$SUFFIX-$$(aws sts get-caller-identity --query Account --output text)"; \
		if aws s3api head-bucket --bucket "$$B" --region $(AWS_REGION) >/dev/null 2>&1; then \
			echo "   emptying s3://$$B"; \
			aws s3 rm "s3://$$B" --recursive --region $(AWS_REGION) --only-show-errors || true; \
		fi; \
	done; \
	echo "   deleting stack $(STACK_NAME)"; \
	if ! aws cloudformation delete-stack --region $(AWS_REGION) --stack-name $(STACK_NAME); then \
		echo ""; \
		echo "❌ The delete request itself failed - CloudFormation was not reached, so"; \
		echo "   nothing has been deleted. This is usually a dropped connection or"; \
		echo "   expired credentials rather than anything wrong with the stack."; \
		echo "   Check credentials and connectivity, then re-run 'make destroy'."; \
		exit 1; \
	fi; \
	aws cloudformation wait stack-delete-complete --region $(AWS_REGION) --stack-name $(STACK_NAME) || { \
		echo ""; \
		echo "⏳ First delete pass did not finish. The usual cause is the agent runtime"; \
		echo "   still holding network interfaces in the private subnets, which blocks the"; \
		echo "   subnets and their security group. Waiting for them, then retrying."; \
		./deployment/destroy/retry-stack-delete.sh "$(STACK_NAME)" "$(AWS_REGION)" 30 || { \
			echo ""; \
			echo "   Other things that can block a delete:"; \
			echo "     - a bucket refilled during teardown (re-run 'make destroy')"; \
			echo "     - the CloudTrail bucket is RETAIN by design; delete it by hand"; \
			exit 1; }; }; \
	echo ""; \
	echo "🗑️  Stack deleted."; \
	echo ""; \
	if [ "$(AWS_REGION)" != "us-east-1" ]; then \
		EDGE_STACK="$(STACK_NAME)-edge-$(AWS_REGION)"; \
		if aws cloudformation describe-stacks --region us-east-1 --stack-name "$$EDGE_STACK" >/dev/null 2>&1; then \
			echo "   deleting CloudFront WAF edge stack $$EDGE_STACK (us-east-1)"; \
			aws cloudformation delete-stack --region us-east-1 --stack-name "$$EDGE_STACK"; \
			aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name "$$EDGE_STACK" 2>/dev/null \
				|| echo "   note: edge stack $$EDGE_STACK did not finish deleting; remove it by hand in us-east-1"; \
		fi; \
	fi; \
	echo ""; \
	AUDIT_RE=$$(PREFIX="$(RESOURCE_PREFIX)" ACCOUNT="$$ACCT" STACK="$(STACK_NAME)" REGION="$(AWS_REGION)" \
		bash -c '. deployment/orphan-names.sh && printf "%s" "$$AUDIT_BUCKET_RE"' 2>/dev/null); \
	for AB in $$( { if [ -n "$$AUDIT_B" ] && [ "$$AUDIT_B" != "None" ]; then echo "$$AUDIT_B"; fi; \
		if [ -n "$$AUDIT_RE" ]; then aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null \
			| tr '\t' '\n' | grep -E "$$AUDIT_RE"; fi; } | sort -u ); do \
		LEFT=$$(aws s3api list-object-versions --bucket "$$AB" --region $(AWS_REGION) --max-keys 1 --no-paginate \
			--query 'sum([length(Versions || `[]`), length(DeleteMarkers || `[]`)])' --output text 2>/dev/null); \
		if [ "$$LEFT" = "0" ]; then \
			if aws s3api delete-bucket --bucket "$$AB" --region $(AWS_REGION) >/dev/null 2>&1; then \
				echo "   deleted empty audit bucket $$AB"; \
			else echo "   audit bucket $$AB: could not delete; left in place"; fi; \
		elif [ -n "$$LEFT" ]; then \
			set -- $$(aws s3api list-object-versions --bucket "$$AB" --region $(AWS_REGION) \
				--query 'max_by(Versions || `[]`, &LastModified).[Key, VersionId]' --output text 2>/dev/null); \
			UNTIL=$$(aws s3api get-object-retention --bucket "$$AB" --region $(AWS_REGION) --key "$$1" \
				--version-id "$$2" --query 'Retention.RetainUntilDate' --output text 2>/dev/null); \
			echo "   audit bucket $$AB: kept until its last record's lock ends (newest record locked until $${UNTIL:-unknown})"; \
		else echo "   audit bucket $$AB: could not inspect; left in place"; fi; \
	done; \
	echo ""; \
	echo "   Left behind on purpose - all carry DeletionPolicy: Retain, so a teardown"; \
	echo "   cannot destroy your onboarding, audit trail or build inputs:"; \
	echo ""; \
	./deployment/check-orphans.sh "$(STACK_NAME)" "$(RESOURCE_PREFIX)" "$(AWS_REGION)" 2>/dev/null \
		| sed -n 's/^     /     /p' || true; \
	echo "     ecr repositories $(RESOURCE_PREFIX)-backend, $(RESOURCE_PREFIX)-batch"; \
	echo "     s3 bucket        $(ARTIFACTS_BUCKET)  (staged templates)"; \
	echo ""; \
	echo "   A redeploy reuses them. To wipe them instead, run 'make purge-orphans'."; \
	echo "   NOTE: leaving them means the next 'make deploy' reuses the same registries."

clean:
	@rm -rf deployment/global-s3-assets deployment/regional-s3-assets deployment/.layer-build
	@rm -f deployment/.artifacts-env deployment/.images-env
	@# Every environment's build state; their deploy.<name>.config settings are kept.
	@rm -rf deployment/.state
	@rm -rf .layer-build backend/.tier1-build
	@rm -f batch/references.yaml
	@rm -rf batch/tools batch/certs
	@rm -rf frontend/dist admin-console/dist
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Local build artifacts removed. AWS resources untouched (see 'make destroy')."
