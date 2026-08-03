.DEFAULT_GOAL := help
SHELL := /bin/bash
CLUSTER := loom
NS := loom
IMAGE_TAG ?= dev

.PHONY: help
help:  ## Liệt kê các lệnh
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: sync
sync:  ## Cài dependency Python
	uv sync --all-packages

.PHONY: lint
lint:  ## ruff + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

.PHONY: fmt
fmt:  ## Tự sửa định dạng
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: test
test:  ## Unit test (không cần Docker)
	uv run pytest

.PHONY: test-int
test-int:  ## Integration test (cần Docker)
	uv run pytest -m integration -o addopts=""

.PHONY: migrate
migrate:  ## Chạy migration lên head
	cd services/api && uv run alembic upgrade head

.PHONY: migration
migration:  ## Sinh migration mới: make migration m="mô tả"
	cd services/api && uv run alembic revision -m "$(m)"

.PHONY: build-api
build-api:  ## Build image loom-api
	docker build -f services/api/Dockerfile -t loom/api:$(IMAGE_TAG) .

.PHONY: web-install
web-install:  ## Cài dependency web
	cd web && npm ci

.PHONY: web-test
web-test:  ## Test frontend
	cd web && npm run test -- --run && npm run typecheck

.PHONY: build-web
build-web:  ## Build image loom-web
	docker build -f web/Dockerfile -t loom/web:$(IMAGE_TAG) .

.PHONY: build
build: build-api build-web  ## Build cả hai image

.PHONY: helm-validate
helm-validate:  ## helm lint + kubeconform cho cả ba môi trường
	helm lint deploy/helm/loom
	@for env in local dev prod; do \
		echo "→ $$env"; \
		helm template loom deploy/helm/loom -n $(NS) \
			-f deploy/envs/values-$$env.yaml \
		| kubeconform -strict -summary \
			-kubernetes-version 1.32.0; \
	done
