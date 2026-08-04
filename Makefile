.DEFAULT_GOAL := help
SHELL := /bin/bash

# Nguồn phiên bản duy nhất. `include` để những giá trị này thật sự được dùng —
# một file pin mà không ai đọc thì chỉ là tài liệu, và sẽ lệch âm thầm.
include deploy/versions.env

# scripts/bootstrap.sh cài k3d/tilt/kubeconform vào ~/.local/bin. Đưa thư mục đó
# vào PATH của make để không phải nhớ sửa ~/.bashrc trước khi chạy target nào.
export PATH := $(HOME)/.local/bin:$(PATH)

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
	@#
	@# `pipefail` cũng BẮT BUỘC, vì lý do khác: `set -e` chỉ thấy lệnh CUỐI của
	@# pipe. Nếu chính `helm template` hỏng (template sai, thiếu `required`,
	@# thiếu file values) thì kubeconform đọc stdin RỖNG, báo "0 resources found"
	@# và thoát 0 — target xanh trong khi không kiểm tra được gì cả.
	@set -eo pipefail; for env in local dev prod; do \
		echo "→ $$env"; \
		helm template loom deploy/helm/loom -n $(NS) \
			-f deploy/envs/values-$$env.yaml \
		| kubeconform -strict -summary \
			-kubernetes-version $(KUBECONFORM_K8S_VERSION); \
	done

.PHONY: bootstrap
bootstrap:  ## Cài k3d, tilt, kubeconform và kiểm tra môi trường
	./scripts/bootstrap.sh

.PHONY: cluster-up
cluster-up:  ## Tạo cụm k3d nếu chưa có
	@# --image đè lên config file: cluster.yaml không có trường `image:` nên nếu
	@# không truyền cờ này thì K3S_IMAGE trong versions.env hoàn toàn vô tác dụng.
	@k3d cluster list -o json | jq -e '.[] | select(.name=="$(CLUSTER)")' >/dev/null 2>&1 \
		|| k3d cluster create --config deploy/k3d/cluster.yaml --image "$(K3S_IMAGE)"
	@# `k3d cluster create` tự chuyển context, nhưng nhánh idempotent (cụm đã có)
	@# thì không — mà máy này có sẵn kubeconfig trỏ vào một cụm thật khác. Chốt
	@# context ở đây để `make infra` phía sau không cần người dùng nhớ đổi tay,
	@# đúng như thông báo của check-context đã hứa.
	kubectl config use-context k3d-$(CLUSTER)
	@# --context tường minh: target này KHÔNG thể phụ thuộc check-context (lúc
	@# gọi thì cụm còn chưa tồn tại), nên phải tự bảo vệ mình.
	kubectl --context k3d-$(CLUSTER) create namespace $(NS) \
	  --dry-run=client -o yaml | kubectl --context k3d-$(CLUSTER) apply -f -

.PHONY: cluster-down
cluster-down:  ## Xoá cụm k3d
	k3d cluster delete $(CLUSTER)

.PHONY: infra
infra: check-context  ## Cài Dex (local — không có ESO). KHÔNG kèm Secret Aiven
	@# Dex dùng config tĩnh của chính nó, không đụng database. Gộp Secret Aiven
	@# vào đây sẽ khiến không dựng nổi tầng đăng nhập chỉ vì chưa có credential.
	@# `make dev` (Task 16) mới là chỗ phụ thuộc cả hai.
	@#
	@# envsubst có tham số '$$DEX_IMAGE' để CHỈ thay đúng biến đó — dex.yaml còn
	@# chứa hash bcrypt đầy ký tự '$', thay bừa là hỏng mật khẩu đăng nhập.
	DEX_IMAGE="$(DEX_IMAGE)" envsubst '$$DEX_IMAGE' < deploy/infra/dex.yaml | kubectl apply -f -
	kubectl -n $(NS) rollout status deployment/dex --timeout=180s

.PHONY: infra-eso
infra-eso: check-context  ## CHỈ dev/prod: cài External Secrets Operator và áp ExternalSecret
	kubectl apply --server-side -f \
	  "https://github.com/external-secrets/external-secrets/releases/download/$(ESO_VERSION)/external-secrets.yaml"
	kubectl -n external-secrets rollout status deployment/external-secrets --timeout=180s
	kubectl apply -f deploy/infra/external-secret.yaml
	kubectl -n $(NS) wait --for=condition=Ready externalsecret/loom-db --timeout=120s

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
