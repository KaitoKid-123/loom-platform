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

.PHONY: check-context
check-context:  ## Chặn mọi lệnh kubectl chạy nhầm vào cụm khác
	@ctx=$$(kubectl config current-context 2>/dev/null || echo none); \
	if [ "$$ctx" != "k3d-$(CLUSTER)" ]; then \
		echo "Context hiện tại: '$$ctx' — không phải 'k3d-$(CLUSTER)'."; \
		echo "Chạy 'make cluster-up' trước, hoặc:"; \
		echo "  kubectl config use-context k3d-$(CLUSTER)"; \
		exit 1; \
	fi

.PHONY: migrate
migrate:  ## Chạy migration lên head (chạy từ host tới Aiven, KHÔNG qua cụm)
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
	@# `set -e` là BẮT BUỘC: không có nó, exit status của cả vòng lặp là của
	@# lần lặp CUỐI (prod), nên một lỗi chỉ xảy ra ở local sẽ không làm target đỏ.
	@set -e; for env in local dev prod; do \
		echo "→ $$env"; \
		helm template loom deploy/helm/loom -n $(NS) \
			-f deploy/envs/values-$$env.yaml \
		| kubeconform -strict -summary \
			-kubernetes-version 1.32.0; \
	done

.PHONY: infra-local-secret
infra-local-secret: check-context  ## CHỈ LOCAL: nạp Secret Aiven từ deploy/local/
	@test -f deploy/local/aiven.env || { \
		echo "Thiếu deploy/local/aiven.env — copy từ aiven.env.example rồi điền"; exit 1; }
	@test -f deploy/local/aiven-ca.pem || { \
		echo "Thiếu deploy/local/aiven-ca.pem — tải CA từ console Aiven"; exit 1; }
	@# kubectl (>=1.30) từ chối kết hợp --from-env-file với --from-file trong
	@# cùng một lệnh ("from-env-file cannot be combined with from-file or
	@# from-literal") — đã kiểm chứng thật trên máy này. Vá bằng jq: dựng
	@# Secret từ --from-env-file trước, rồi ghép khoá ca.pem vào bằng
	@# --rawfile (đọc file trực tiếp, nội dung KHÔNG bao giờ thành một đối số
	@# dòng lệnh nên không lộ qua `ps`).
	kubectl -n $(NS) create secret generic loom-db-app \
	  --from-env-file=deploy/local/aiven.env \
	  --dry-run=client -o json \
	| jq --rawfile ca deploy/local/aiven-ca.pem '.data["ca.pem"] = ($$ca | @base64)' \
	| kubectl apply -f -
