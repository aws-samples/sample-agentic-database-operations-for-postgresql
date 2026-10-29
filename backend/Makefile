# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Local development only.
#
# Deployment lives in the root Makefile and drives one model: the nested stack at
# infrastructure/master.yaml. The image build, artifact staging and stack deploy
# targets that used to live here are gone - use `make deploy` from the repository
# root, or the individual steps `make stage` / `make images` / `make deploy-stack`.

.PHONY: help dev stop

CONTAINER_CMD := $(shell command -v docker 2>/dev/null || command -v finch 2>/dev/null || command -v podman 2>/dev/null || command -v nerdctl 2>/dev/null)
COMPOSE_CMD := $(shell command -v docker-compose 2>/dev/null || echo "$(CONTAINER_CMD) compose")

help:
	@echo ""
	@echo "  make dev    Run the agent locally in a container on port 8084"
	@echo "  make stop   Stop it"
	@echo ""
	@echo "  Requires backend/.env — generate it from the root: make env"
	@echo "  To deploy to AWS, use the root Makefile: make deploy"
	@echo ""

dev:
	@if [ ! -f .env ]; then \
		echo "❌ backend/.env not found. Generate it from the repository root: make env"; \
		exit 1; \
	fi
	@if [ -z "$(CONTAINER_CMD)" ]; then \
		echo "❌ No container runtime found (docker, finch, podman or nerdctl)."; \
		exit 1; \
	fi
	@echo "🚀 Starting the agent on port 8084..."
	$(COMPOSE_CMD) up --build

stop:
	@echo "🛑 Stopping..."
	$(COMPOSE_CMD) down
