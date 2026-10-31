# Thin wrappers around docker compose. Nothing here needs Python on the host.
COMPOSE ?= docker compose
EXEC = $(COMPOSE) exec curator curator
API ?= http://127.0.0.1:8090

.PHONY: build test up down logs status run collect picks shell multiarch

build:
	$(COMPOSE) build curator

test:
	$(COMPOSE) --profile dev build test
	$(COMPOSE) --profile dev run --rm test

up: build
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f curator

status:
	curl -s $(API)/api/status

# Start a job in the running service.
run collect:
	curl -s -X POST $(API)/api/jobs/$@

picks:
	curl -s $(API)/api/reports/latest

shell:
	$(COMPOSE) exec curator bash

multiarch:
	docker buildx build --platform linux/amd64,linux/arm64 --target runtime -t ghcr.io/warroyo/content-curator:dev .
