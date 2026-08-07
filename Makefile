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
	@# Chỉ quét chính Makefile này, KHÔNG phải cả $(MAKEFILE_LIST): từ khi
	@# `include deploy/versions.env` xuất hiện, danh sách có hai file nên grep
	@# gắn tiền tố "Makefile:" vào mọi dòng và awk cắt nhầm — mọi target đều
	@# hiện tên là "Makefile".
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(firstword $(MAKEFILE_LIST)) \
		| awk -F':.*?## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

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
	@# `not benchmark`: phép đo mất vài phút và không khẳng định gì — nó thuộc
	@# `make measure-scan`, không thuộc cổng kiểm chạy mỗi lần push.
	uv run pytest -m "integration and not benchmark" -o addopts=""

.PHONY: check-context
check-context:  ## Chặn mọi lệnh kubectl chạy nhầm vào cụm khác
	@ctx=$$(kubectl config current-context 2>/dev/null || echo none); \
	if [ "$$ctx" != "k3d-$(CLUSTER)" ]; then \
		echo "Context hiện tại: '$$ctx' — không phải 'k3d-$(CLUSTER)'."; \
		echo "Chạy 'make cluster-up' trước, hoặc:"; \
		echo "  kubectl config use-context k3d-$(CLUSTER)"; \
		exit 1; \
	fi


# Nạp credential Aiven cho các lệnh alembic chạy TỪ HOST. Trong cụm thì pod đã có
# sẵn qua Secret loom-db-app; từ host thì không có gì nạp hộ, và thiếu chúng thì
# alembic chết bằng một stack trace asyncpg thay vì một câu tiếng người.
#
# PGSSLROOTCERT là bắt buộc riêng: db_sslmode mặc định `verify-full`, và không có
# biến này asyncpg đi tìm ~/.postgresql/root.crt rồi báo
# "root certificate file does not exist" — đúng cái bẫy đã ghi từ Giai đoạn 0.
#
# Tên khoá trong aiven.env là username/password/host/port/dbname để khớp khoá của
# Secret trong Kubernetes; Settings thì đọc LOOM_DB_*. Ánh xạ ở đây.
define DB_ENV
test -f deploy/local/aiven.env || { \
    echo "Thiếu deploy/local/aiven.env — copy từ aiven.env.example rồi điền"; exit 1; }; \
test -f deploy/local/aiven-ca.pem || { \
    echo "Thiếu deploy/local/aiven-ca.pem — tải CA từ console Aiven"; exit 1; }; \
set -a; . ./deploy/local/aiven.env; set +a; \
export LOOM_DB_USER="$$username" LOOM_DB_PASSWORD="$$password" \
       LOOM_DB_HOST="$$host" LOOM_DB_PORT="$$port" LOOM_DB_NAME="$$dbname" \
       PGSSLROOTCERT="$$PWD/deploy/local/aiven-ca.pem";
endef

.PHONY: migrate
migrate:  ## Chạy migration lên head (chạy từ host tới Aiven, KHÔNG qua cụm)
	@$(DB_ENV) cd services/api && uv run alembic upgrade head

.PHONY: grant-admin
grant-admin:  ## Gán admin cấp tenant cho admin ĐẦU TIÊN: make grant-admin EMAIL=...
	@# Mọi thứ khác cấp quyền qua API, và API đòi người gọi đã có quyền. Cái đầu
	@# tiên không có ai cấp được, nên nó phải đến từ ngoài hệ thống.
	@test -n "$(EMAIL)" || { echo "Thiếu EMAIL:  make grant-admin EMAIL=long@loom.local"; exit 1; }
	@$(DB_ENV) uv run python scripts/grant_tenant_admin.py "$(EMAIL)"

.PHONY: migration
migration:  ## Sinh migration mới: make migration m="mô tả"
	cd services/api && uv run alembic revision -m "$(m)"

.PHONY: check-migrations
check-migrations:  ## Chặn model và migration lệch nhau (cần database, LOOM_DB_* trong env)
	@# Giai đoạn 0 từng có index trong migration mà thiếu trong model, nên
	@# autogenerate lần sau sẽ DROP chúng. `alembic check` so model với database.
	@#
	@# LƯU Ý: `alembic check` KHÔNG so server_default (migrations/env.py không đặt
	@# compare_server_default), nên xanh ở đây KHÔNG chứng minh mọi server_default
	@# khớp. Nó cũng không so CHECK constraint. Hai khoảng đó được
	@# tests/integration/test_migrations.py bịt bằng cách đọc thẳng pg_constraint
	@# và bằng phép kiểm hành vi trên schema đã migrate.
	@$(DB_ENV) cd services/api && uv run alembic check

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

.PHONY: build-query
build-query:  ## Build image loom-query
	docker build -f services/loom-query/Dockerfile -t loom/query:$(IMAGE_TAG) .

.PHONY: build
build: build-api build-web build-query  ## Build cả ba image

.PHONY: helm-validate
helm-validate:  ## helm lint + kubeconform cho ba môi trường và dex.yaml
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
	@# dex.yaml không nằm trong chart nên vòng lặp trên không chạm tới nó. Kiểm
	@# bản ĐÃ render qua envsubst, đúng thứ mà `make infra` đưa vào cụm: bản thô
	@# cũng hợp lệ với kubeconform (`image: $${DEX_IMAGE}` chỉ là một string) nên
	@# kiểm bản thô không nói lên điều gì về thứ thật sự được apply.
	@#
	@# deploy/infra/external-secret.yaml CỐ Ý không có ở đây: ClusterSecretStore
	@# và ExternalSecret là CRD, không có schema trong catalog mặc định của
	@# kubeconform. Thêm nó vào cần -schema-location trỏ ra kho CRD ngoài; chưa
	@# làm, nên file đó hiện KHÔNG được CI kiểm.
	@echo "→ infra/dex.yaml"
	@set -eo pipefail; DEX_IMAGE="$(DEX_IMAGE)" envsubst '$$DEX_IMAGE' < deploy/infra/dex.yaml \
	| kubeconform -strict -summary \
		-kubernetes-version $(KUBECONFORM_K8S_VERSION)

	@echo "→ infra/minio.yaml"
	@# Cùng lý do với dex.yaml: kiểm bản ĐÃ render, vì bản thô có `image:
	@# $${MINIO_IMAGE}` cũng hợp lệ với kubeconform nên kiểm nó không nói lên gì.
	@set -eo pipefail; \
	MINIO_IMAGE="$(MINIO_IMAGE)" MINIO_MC_IMAGE="$(MINIO_MC_IMAGE)" \
	envsubst '$$MINIO_IMAGE $$MINIO_MC_IMAGE' < deploy/infra/minio.yaml \
	| kubeconform -strict -summary \
		-kubernetes-version $(KUBECONFORM_K8S_VERSION)

	@echo "→ lakekeeper: initContainer và container phải cùng database"
	@# helm lint và kubeconform đều KHÔNG thấy được lỗi này, mà nó là lỗi tệ nhất
	@# có thể xảy ra: initContainer `migrate` trỏ nhầm sang database của control
	@# plane sẽ chạy migration của Lakekeeper lên schema của loom-api.
	@set -eo pipefail; \
	keys=$$(helm template loom deploy/helm/loom -n $(NS) \
		-f deploy/envs/values-local.yaml \
		| grep -A4 'name: LAKEKEEPER__PG_DATABASE' \
		| grep 'key:' | awk '{print $$2}' | sort -u); \
	nkeys=$$(echo "$$keys" | grep -c .); \
	test "$$nkeys" -eq 1 || { \
		echo "initContainer và container trỏ KHÁC database: $$keys"; exit 1; }; \
	want=$$(grep 'lakekeeperNameKey:' deploy/helm/loom/values.yaml | awk '{print $$2}'); \
	test "$$keys" = "$$want" || { \
		echo "khoá database sai: dùng '$$keys', values.yaml khai '$$want'"; exit 1; }; \
	echo "  cả hai dùng khoá $$keys"

	@echo "→ chart: mọi tài nguyên phải tự khai namespace"
	@# `kubectl apply -f <ban render>` đặt tài nguyên KHÔNG khai namespace vào
	@# namespace mặc định của context. Tilt và ArgoCD đều tự tiêm namespace nên
	@# khoảng trống này im lặng — cho tới khi ai đó apply tay và mọi thứ lặng lẽ
	@# vào `default`. Đã dính đúng thế một lần ở Task 14.
	@set -eo pipefail; \
	missing=$$(helm template loom deploy/helm/loom -n $(NS) \
		-f deploy/envs/values-local.yaml \
		| awk '/^kind:/{k=$$2} /^  name:/{n=$$2; has=0} \
		       /^  namespace:/{has=1} \
		       /^---$$/{if(k && !has) print k" "n; k=""; n=""; has=0} \
		       END{if(k && !has) print k" "n}'); \
	test -z "$$missing" || { \
		echo "thiếu namespace: $$missing"; exit 1; }; \
	echo "  mọi tài nguyên đều khai namespace"

	@echo "→ argocd/"
	@# ArgoCD Application là CRD nên không có trong catalog mặc định của
	@# kubeconform. Cách thường thấy là thêm -ignore-missing-schemas, nhưng thế
	@# thì mọi Application thành "Skipped" và một apiVersion gõ sai vẫn exit 0 —
	@# đã kiểm: Skipped 2, exit 0, không bắt được gì. Nên vendor hẳn schema vào
	@# deploy/schemas/ và KHÔNG dùng cờ đó: thiếu schema giờ là Errors, không
	@# phải Skipped.
	kubeconform -strict -summary \
		-schema-location 'deploy/schemas/{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json' \
		-kubernetes-version $(KUBECONFORM_K8S_VERSION) deploy/argocd/

.PHONY: check-pins
check-pins:  ## Chặn FROM trong Dockerfile lệch với deploy/versions.env
	@# CI cài node và uv theo NODE_VERSION/UV_VERSION trong deploy/versions.env,
	@# nhưng hai Dockerfile mang tag riêng của chúng. Không có target này thì hai
	@# bên trôi khỏi nhau âm thầm và CI đi kiểm một toolchain không ai build bằng.
	@grep -q '^FROM node:$(NODE_VERSION)-alpine' web/Dockerfile \
		|| { echo "web/Dockerfile không FROM node:$(NODE_VERSION)-alpine — lệch NODE_VERSION trong deploy/versions.env"; exit 1; }
	@grep -q 'astral-sh/uv:$(UV_VERSION)-' services/api/Dockerfile \
		|| { echo "services/api/Dockerfile không dùng uv:$(UV_VERSION) — lệch UV_VERSION trong deploy/versions.env"; exit 1; }
	@grep -q 'astral-sh/uv:$(UV_VERSION)-' services/loom-query/Dockerfile \
		|| { echo "services/loom-query/Dockerfile không dùng uv:$(UV_VERSION) — lệch UV_VERSION trong deploy/versions.env"; exit 1; }
	@echo "Pin khớp: node $(NODE_VERSION), uv $(UV_VERSION)"

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
infra: check-context  ## Cài Dex + MinIO (local — không có ESO). KHÔNG kèm Secret Aiven
	@# Dex dùng config tĩnh của chính nó, không đụng database. Gộp Secret Aiven
	@# vào đây sẽ khiến không dựng nổi tầng đăng nhập chỉ vì chưa có credential.
	@# `make dev` (Task 16) mới là chỗ phụ thuộc cả hai.
	@#
	@# envsubst giới hạn ở '$$DEX_IMAGE' để mọi chuỗi trông-giống-biến thêm vào
	@# dex.yaml sau này không bị thay bằng rỗng một cách âm thầm.
	@# LƯU Ý: kubeconform KHÔNG bắt được lỗi ở đây — `image` là trường tuỳ chọn
	@# nên bản thô, bản render đúng và bản render RỖNG đều hợp lệ như nhau. Việc
	@# render trước khi validate chỉ có ý nghĩa nếu sau này thêm giữ chỗ ở vị trí
	@# ảnh hưởng cấu trúc.
	@# (Hash bcrypt hiện tại KHÔNG cần sự bảo vệ này: mọi '$' trong hash đều
	@# theo sau bởi chữ số — '$2a', '$10' — mà '$<số>' không phải tham chiếu
	@# biến hợp lệ nên envsubst bỏ qua. Đã kiểm: bản có và không có giới hạn
	@# cho ra kết quả giống hệt nhau. Giữ giới hạn vì nó phòng tương lai.)
	DEX_IMAGE="$(DEX_IMAGE)" envsubst '$$DEX_IMAGE' < deploy/infra/dex.yaml | kubectl apply -f -
	@# MinIO CHỈ ở local — dev/prod trỏ endpoint ngoài (VPS), xem spec GĐ 2 mục 2.0.
	MINIO_IMAGE="$(MINIO_IMAGE)" MINIO_MC_IMAGE="$(MINIO_MC_IMAGE)" \
	envsubst '$$MINIO_IMAGE $$MINIO_MC_IMAGE' < deploy/infra/minio.yaml | kubectl apply -f -
	kubectl -n $(NS) rollout status deployment/minio --timeout=180s
	@# Chờ Job, không chỉ chờ Deployment. Không có dòng này thì `make infra` báo
	@# thành công kể cả khi bucket chưa từng được tạo, và lỗi chỉ lộ ra ở Task 6
	@# dưới dạng một 404 không nói gì về nguyên nhân. Đã chứng minh đỏ được: trỏ
	@# Job vào một host không tồn tại thì target này thoát Error 1.
	@#
	@# `--for=condition=complete` KHÔNG thoát sớm khi Job chuyển sang Failed — nó
	@# chờ hết timeout. Đường thành công vẫn trả về ngay, chỉ đường hỏng mới chậm,
	@# nên đổi lấy sự đơn giản là đáng.
	kubectl -n $(NS) wait --for=condition=complete job/minio-bucket --timeout=300s
	@# Dex đọc config.yaml MỘT LẦN lúc khởi động. Sửa ConfigMap thôi thì `kubectl
	@# apply` báo "configmap configured / deployment unchanged", `rollout status`
	@# báo thành công, và Dex vẫn chạy cấu hình cũ — target nói "đã áp" trong khi
	@# thực tế chưa. Restart vô điều kiện để "đã áp" nghĩa là "đang chạy".
	kubectl -n $(NS) rollout restart deployment/dex
	kubectl -n $(NS) rollout status deployment/dex --timeout=180s

.PHONY: minio-console
minio-console: check-context  ## Mở console MinIO ở http://localhost:9001
	@echo "Console: http://localhost:9001  —  loom-root / loom-root-dev-only"
	kubectl -n $(NS) port-forward svc/minio 9001:9001

.PHONY: minio-s3
minio-s3: check-context  ## Port-forward cổng S3 của MinIO ra localhost:9000
	@echo "S3: http://localhost:9000  —  loom-root / loom-root-dev-only"
	kubectl -n $(NS) port-forward svc/minio 9000:9000

.PHONY: infra-eso
infra-eso:  ## CHỈ dev/prod: ESO + ExternalSecret. Bắt buộc: make infra-eso CONTEXT=...
	@# KHÔNG dùng check-context được: target này dành cho dev/prod, mà
	@# check-context lại đòi đúng k3d-loom — gắn vào thì nó chỉ chạy được trên
	@# đúng cụm mà nó không phục vụ. Thay bằng: bắt chỉ rõ context, và từ chối
	@# thẳng cụm local.
	@test -n "$(CONTEXT)" || { \
		echo "Phải chỉ rõ cụm đích:  make infra-eso CONTEXT=<tên context dev/prod>"; \
		echo "Xem danh sách:  kubectl config get-contexts -o name"; exit 1; }
	@test "$(CONTEXT)" != "k3d-$(CLUSTER)" || { \
		echo "'$(CONTEXT)' là cụm local. Local không với tới Vault nên dùng"; \
		echo "'make infra-local-secret' thay vì ESO."; exit 1; }
	kubectl --context $(CONTEXT) apply --server-side -f \
	  "https://github.com/external-secrets/external-secrets/releases/download/$(ESO_VERSION)/external-secrets.yaml"
	kubectl --context $(CONTEXT) -n external-secrets rollout status deployment/external-secrets --timeout=180s
	kubectl --context $(CONTEXT) apply -f deploy/infra/external-secret.yaml
	kubectl --context $(CONTEXT) -n $(NS) wait --for=condition=Ready externalsecret/loom-db --timeout=120s

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

.PHONY: dev
dev: cluster-up infra infra-local-secret  ## Dựng mọi thứ rồi chạy Tilt
	@# `infra-local-secret` nằm ở đây chứ không nằm trong `infra`, đúng như ghi
	@# chú của target đó: Dex phải dựng được kể cả khi chưa có credential Aiven,
	@# còn `make dev` thì cần cả hai. Đặt nó là prerequisite cũng để lỗi "thiếu
	@# deploy/local/*" hiện ra ngay dưới dạng một câu tiếng người, thay vì thành
	@# một pod loom-api treo ở ContainerCreating trong giao diện Tilt.
	tilt up

.PHONY: dev-down
dev-down: check-context  ## Dừng Tilt và gỡ tài nguyên do nó tạo
	tilt down

.PHONY: lint-workflows
lint-workflows:  ## actionlint + shellcheck cho .github/workflows
	@# -shellcheck tường minh: nếu binary không có, actionlint bỏ qua luật đó
	@# và vẫn trả 0. Chỉ rõ đường dẫn để thiếu là BÁO LỖI chứ không phải im lặng.
	@command -v shellcheck >/dev/null || { \
		echo "Thiếu shellcheck — chạy 'make bootstrap'. Không có nó, actionlint"; \
		echo "bỏ qua luật shellcheck và báo xanh oan."; exit 1; }
	actionlint -shellcheck "$$(command -v shellcheck)"

.PHONY: smoke
smoke:  ## Bảy phép kiểm chấp nhận qua HTTP (BASE=... để chạy với môi trường khác)
	@./scripts/smoke.sh

.PHONY: lint-shell
lint-shell:  ## shellcheck cho mọi script shell trong repo
	@# lint-workflows chỉ quét .github/workflows. Ba script shell của dự án
	@# (bootstrap, smoke, git hook) trước đó không được lint bởi bất cứ thứ gì.
	@command -v shellcheck >/dev/null || { \
		echo "Thiếu shellcheck — chạy 'make bootstrap'."; exit 1; }
	shellcheck scripts/*.sh scripts/git-hooks/*

.PHONY: measure-spill
measure-spill:  ## Phép đo 1 mục 3 (CỬA CHẶN) — DuckDB trong cgroup 384Mi thật
	@# Chạy trong một container có ĐÚNG limit mà Giai đoạn 2b sẽ đặt cho
	@# loom-query. Chạy trên host thì không có cgroup nào để bị giết, và bài đo
	@# xanh mà không chứng minh được gì.
	docker run --rm --memory=384m --memory-swap=384m \
		-v "$(PWD):/w" -w /w python:3.12-slim \
		sh -c "pip install --quiet duckdb && python scripts/measure_duckdb_spill.py"

.PHONY: measure-scan
measure-scan:  ## Phép đo 1 mục 1 — thời gian lập kế hoạch quét bảng Iceberg
	@# Dùng chính bộ container mà integration test dùng, không dựng bộ thứ hai:
	@# một môi trường khác cho ra một con số nói về hệ thống không ai chạy.
	uv run pytest -m benchmark -o addopts="" -s \
		packages/icebergkit/tests/integration/test_scan_planning_benchmark.py

.PHONY: ram
ram: check-context  ## Tổng RAM cụm đang dùng, so với trần 1,8 GB
	@# Cộng trên giấy ở spec Giai đoạn 2 mục 7.3 là ước lượng từ
	@# `resources.requests`, KHÔNG phải mức dùng thật. Đây là số thật, đọc từ
	@# cgroup của node — KHÔNG qua `kubectl exec`.
	@#
	@# Bản đầu exec vào từng pod rồi `cat /sys/fs/cgroup/memory.current`. Nó báo
	@# Lakekeeper dùng 0 Mi, và 0 là SAI: image distroless không có `cat`, exec
	@# hỏng, và target lặng lẽ cộng 0 vào tổng. Một phép đo báo thiếu thì tệ hơn
	@# không đo, vì nó cho một con số trông như đã kiểm.
	@#
	@# Đọc từ node thì không phụ thuộc trong container có shell hay không.
	@# So với TRẦN phải là số của CẢ NODE, không phải của riêng namespace $(NS):
	@# k3s, traefik, coredns, local-path-provisioner đều ăn vào cùng trần đó.
	@node=k3d-$(CLUSTER)-server-0; \
	total=0; seen=0; \
	for id in $$(docker exec $$node crictl ps -q 2>/dev/null); do \
	  name=$$(docker exec $$node crictl inspect --output go-template \
	          --template '{{ .status.labels }}' $$id 2>/dev/null \
	        | grep -o 'io.kubernetes.pod.name:[^ ]*' | cut -d: -f2); \
	  ns=$$(docker exec $$node crictl inspect --output go-template \
	        --template '{{ .status.labels }}' $$id 2>/dev/null \
	      | grep -o 'io.kubernetes.pod.namespace:[^ ]*' | cut -d: -f2); \
	  [ "$$ns" = "$(NS)" ] || continue; \
	  pid=$$(docker exec $$node crictl inspect --output go-template \
	         --template '{{ .info.pid }}' $$id 2>/dev/null); \
	  [ -n "$$pid" ] && [ "$$pid" != "0" ] || { \
	     printf '  %-34s   ĐỌC HỎNG\n' "$$name"; continue; }; \
	  cg=$$(docker exec $$node cat /proc/$$pid/cgroup 2>/dev/null | head -1 | cut -d: -f3); \
	  m=$$(docker exec $$node cat "/sys/fs/cgroup$$cg/memory.current" 2>/dev/null); \
	  [ -n "$$m" ] || { printf '  %-34s   ĐỌC HỎNG\n' "$$name"; continue; }; \
	  mib=$$(( m / 1048576 )); total=$$(( total + mib )); seen=$$(( seen + 1 )); \
	  printf '  %-34s %5d Mi\n' "$$name" "$$mib"; \
	done; \
	printf '\n  Namespace $(NS): %d Mi (%d container)\n' "$$total" "$$seen"; \
	node_mib=$$(docker stats --no-stream --format '{{ .MemUsage }}' $$node \
	  | awk '{v=$$1; sub(/GiB/,"",v); if ($$1 ~ /GiB/) print int(v*1024); \
	          else {sub(/MiB/,"",v); print int(v)}}'); \
	printf '  CẢ NODE:         %d Mi   trần 1843 Mi\n' "$$node_mib"; \
	printf '  còn dư %d Mi — Giai đoạn 2b thêm loom-query, đỉnh đo được 348 Mi\n' \
	  $$(( 1843 - node_mib )); \
	if [ "$$node_mib" -gt 1843 ]; then \
	  echo "  VƯỢT TRẦN — xem spec Giai đoạn 2 mục 7.3, ba lối ra"; exit 1; fi
