.DEFAULT_GOAL := help
SHELL := /bin/bash
CLUSTER := loom
NS := loom

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

.PHONY: web-install
web-install:  ## Cài dependency web
	cd web && npm ci

.PHONY: web-test
web-test:  ## Test frontend
	cd web && npm run test -- --run && npm run typecheck
