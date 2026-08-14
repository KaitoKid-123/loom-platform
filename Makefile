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
web-test: bundle-check  ## Test frontend (kèm phép canh Monaco tải trì hoãn)
	cd web && npm run test -- --run && npm run typecheck

.PHONY: bundle-check
bundle-check:  ## Canh: chunk khởi đầu KHÔNG chứa Monaco (Giai đoạn 2c, xem web/scripts/check-bundle-splitting.mjs)
	cd web && npm run build
	cd web && node scripts/check-bundle-splitting.mjs

.PHONY: build-web
build-web:  ## Build image loom-web
	docker build -f web/Dockerfile -t loom/web:$(IMAGE_TAG) .

.PHONY: build-query
build-query:  ## Build image loom-query
	docker build -f services/loom-query/Dockerfile -t loom/query:$(IMAGE_TAG) .

.PHONY: build-task
build-task:  ## Build image loom-task (chạy một lần rồi chết, không phải server)
	docker build -f services/loom-task/Dockerfile -t loom/task:$(IMAGE_TAG) .

.PHONY: build
build: build-api build-web build-query build-task  ## Build cả bốn image

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

	@echo "→ chart: service nào cần credential gốc thì phải NHẬN được nó"
	@# Lọt một lần thật: `query-deployment.yaml` thiếu hẳn bốn biến STORAGE_*
	@# trong khi `api-deployment.yaml` có đủ. `helm lint` và `kubeconform` đều
	@# xanh — manifest hợp lệ, chỉ là service chạy bằng credential giữ chỗ, và
	@# điều đó chỉ lộ ra khi có người đọc `Files/` thật.
	@set -eo pipefail; \
	rendered=$$(helm template loom deploy/helm/loom -n $(NS) -f deploy/envs/values-local.yaml); \
	for pair in "loom-api:LOOM_" "loom-query:LOOM_QUERY_"; do \
		dep=$${pair%%:*}; pfx=$${pair##*:}; \
		block=$$(echo "$$rendered" | awk "/name: $$dep\$$/,/^---/"); \
		for suffix in STORAGE_ENDPOINT STORAGE_BUCKET STORAGE_ROOT_ACCESS_KEY STORAGE_ROOT_SECRET_KEY; do \
			echo "$$block" | grep -q "name: $$pfx$$suffix" || { \
				echo "$$dep thiếu $$pfx$$suffix"; exit 1; }; \
		done; \
	done; \
	echo "  api và query đều nhận đủ bốn biến storage"

	@echo "→ chart: mọi tài nguyên phải tự khai namespace"
	@# `kubectl apply -f <ban render>` đặt tài nguyên KHÔNG khai namespace vào
	@# namespace mặc định của context. Tilt và ArgoCD đều tự tiêm namespace nên
	@# khoảng trống này im lặng — cho tới khi ai đó apply tay và mọi thứ lặng lẽ
	@# vào `default`. Đã dính đúng thế một lần ở Task 14.
	@#
	@# Phạm vi bắt buộc GIỚI HẠN vào bên trong khối `metadata:` cấp cao nhất
	@# (cờ `im`, bật ở dòng `metadata:` thụt lề 0, tắt ở khoá thụt lề 0 kế
	@# tiếp): api-rbac.yaml (Task 8) thêm `RoleBinding` đầu tiên của chart, và
	@# `roleRef.name` của nó thụt lề 2 khoảng trắng — HỆT `metadata.name`. Bản
	@# awk cũ không phân biệt hai chỗ đó, nên `roleRef.name` (xuất hiện SAU
	@# `metadata.namespace`) âm thầm reset `has` về 0 mà không còn dòng
	@# `namespace:` nào theo sau để bật lại — báo thiếu namespace trên một
	@# RoleBinding đã khai namespace đầy đủ.
	@set -eo pipefail; \
	missing=$$(helm template loom deploy/helm/loom -n $(NS) \
		-f deploy/envs/values-local.yaml \
		| awk '/^kind:/{k=$$2} \
		       /^metadata:$$/{im=1; next} \
		       im && /^[A-Za-z]/{im=0} \
		       im && /^  name:/{n=$$2; has=0} \
		       im && /^  namespace:/{has=1} \
		       /^---$$/{if(k && !has) print k" "n; k=""; n=""; has=0; im=0} \
		       END{if(k && !has) print k" "n}'); \
	test -z "$$missing" || { \
		echo "thiếu namespace: $$missing"; exit 1; }; \
	echo "  mọi tài nguyên đều khai namespace"

	@echo "→ chart: Role của loom-api chỉ cấp jobs/pods, không hơn không kém"
	@# Canh CẢ HAI chiều cho api-rbac.yaml (xem lý do chi tiết trong chính file
	@# đó). Chiều RỘNG: `secrets` (dỡ bỏ lời hứa `SECRET_REF_RE` — một lỗi SQL
	@# injection trong API sẽ đọc được mật khẩu database NGUỒN), `deployments`
	@# (workload sống ngoài vòng đời một run), hay resource "*" đều phải FAIL.
	@#
	@# Chiều HẸP — dễ bị bỏ quên nhất: nếu Role không còn cấp `jobs` thì CŨNG
	@# phải FAIL. Một phép canh chỉ nhìn chiều rộng vẫn xanh khi Role bị làm
	@# rỗng hoặc đổi tên nhầm resource — lúc đó nó đang canh một quyền không
	@# còn hoạt động, và triệu chứng chỉ lộ ra sau, dưới dạng mọi run nạp kẹt
	@# mãi ở `pending` (403 khi `JobLauncher.launch` gọi
	@# `create_namespaced_job`) mà không một dòng log nào chỉ thẳng vào RBAC.
	@set -eo pipefail; \
	role=$$(helm template loom deploy/helm/loom -n $(NS) -f deploy/envs/values-local.yaml \
		| awk '/^kind: Role$$/{f=1} f{print} f&&/^---$$/{exit}'); \
	test -n "$$role" || { \
		echo "không tìm thấy Role nào trong chart — loom-api mất hết quyền k8s"; exit 1; }; \
	resources=$$(echo "$$role" | awk '/^[[:space:]]*resources:[[:space:]]*$$/{inres=1;next} inres&&/^[[:space:]]*-/{print;next} {inres=0}'); \
	echo "$$resources" | grep -qiE '"?(secrets|deployments)"?|"?\*"?' && { \
		echo "Role quá RỘNG — cấp quyền ngoài phạm vi jobs/pods:"; echo "$$resources"; exit 1; }; \
	echo "$$resources" | grep -qw jobs || { \
		echo "Role thiếu quyền 'jobs' — ingest sẽ kẹt mãi ở pending, không có gợi ý RBAC nào trong log"; exit 1; }; \
	echo "  Role chỉ cấp: $$(echo "$$resources" | tr -d ' -' | tr '\n' ' ')"

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
	@grep -q 'astral-sh/uv:$(UV_VERSION)-' services/loom-task/Dockerfile \
		|| { echo "services/loom-task/Dockerfile không dùng uv:$(UV_VERSION) — lệch UV_VERSION trong deploy/versions.env"; exit 1; }
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

.PHONY: infra-local-source-secret
infra-local-source-secret: check-context  ## CHỈ LOCAL: Secret NGUỒN cho pod nạp (Giai đoạn 3a)
	@# Secret mà `ConnectionDefinition.secret_ref` của một connection LOCAL trỏ
	@# tới, và là thứ `JobLauncher.launch` chiếu NGUYÊN KHỐI vào pod nạp bằng
	@# `envFrom`. Vì `envFrom` biến MỌI khoá thành một biến môi trường, tên khoá
	@# ở đây LÀ tên biến mà `loom_task.config.SourceCredentials` đọc
	@# (`env_prefix="LOOM_TASK_"` + `source_user`/`source_password`). Đó là cả
	@# quy ước, và nó không có chỗ nào khác để được khai báo — không có template
	@# Helm nào sinh Secret này, vì mỗi connection có Secret RIÊNG do người vận
	@# hành tạo.
	@#
	@# KHÔNG dùng lại `loom-db-app`: nó mang khoá `ca.pem`, một cái tên KHÔNG
	@# hợp lệ cho biến môi trường, nên kubelet bỏ qua khoá đó và ghi một
	@# `InvalidVariableNames` vào event của pod — còn `username`/`password` thì
	@# sai tên với `SourceCredentials` nên pod vẫn chết vì thiếu credential.
	@#
	@# Credential ở đây là credential Aiven THẬT, và điều đó là bắt buộc chứ
	@# không tiện tay: cụm local không có Postgres nào khác để nạp TỪ (database
	@# của Loom là dịch vụ ngoài ở cả ba môi trường — xem `database` trong
	@# values.yaml), nên một credential giả sẽ chỉ chứng minh được đường báo
	@# lỗi. Nó đọc từ `deploy/local/aiven.env` (gitignore) đúng cách
	@# `infra-local-secret` đọc, và KHÔNG BAO GIỜ đi qua dòng lệnh: `jq` đổi tên
	@# khoá trên JSON mà `kubectl --dry-run` sinh ra, nên giá trị không lộ qua
	@# `ps`.
	@test -f deploy/local/aiven.env || { \
		echo "Thiếu deploy/local/aiven.env — copy từ aiven.env.example rồi điền"; exit 1; }
	kubectl -n $(NS) create secret generic loom-source-local \
	  --from-env-file=deploy/local/aiven.env \
	  --dry-run=client -o json \
	| jq '.data |= {LOOM_TASK_SOURCE_USER: .username, LOOM_TASK_SOURCE_PASSWORD: .password}' \
	| kubectl apply -f -

.PHONY: dev
dev: cluster-up infra infra-local-secret infra-local-source-secret  ## Dựng mọi thứ rồi chạy Tilt
	@# Hai target `infra-local-*` nằm ở đây chứ không nằm trong `infra`, đúng như
	@# ghi chú của chúng: Dex phải dựng được kể cả khi chưa có credential Aiven,
	@# còn `make dev` thì cần cả hai. Đặt chúng là prerequisite cũng để lỗi
	@# "thiếu deploy/local/*" hiện ra ngay dưới dạng một câu tiếng người, thay vì
	@# thành một pod loom-api treo ở ContainerCreating trong giao diện Tilt —
	@# hoặc, với Secret nguồn, một pod NẠP treo ở CreateContainerConfigError ở
	@# lần nạp đầu tiên, xa hẳn nguyên nhân.
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
smoke:  ## Mười bốn phép kiểm chấp nhận qua HTTP (BASE=... để chạy với môi trường khác)
	@# Phép 14 nạp từ một Postgres NGUỒN thật, nên nó cần host/port/dbname của
	@# nguồn đó. Ở local nguồn duy nhất cụm với tới được là chính Aiven (không có
	@# Postgres nào trong cụm — xem `database` ở values.yaml), và địa chỉ của nó
	@# nằm trong `deploy/local/aiven.env`, file GITIGNORE. Đọc ở ĐÂY rồi truyền
	@# vào môi trường của script, thay vì viết cứng trong `scripts/smoke.sh`: file
	@# đó nằm trong một repo công khai.
	@#
	@# Chỉ ba khoá không phải bí mật (host/port/dbname) được lấy ra — `password`
	@# KHÔNG đi vào môi trường của smoke, vì smoke không mở kết nối nào tới nguồn
	@# (pod nạp mới mở, và nó lấy credential từ Secret `loom-source-local`).
	@#
	@# `:-` giữ quyền ưu tiên cho biến người dùng đặt sẵn: `make smoke
	@# BASE=https://loom-dev.internal SMOKE_SOURCE_HOST=...` không bị file local
	@# ghi đè.
	@f=deploy/local/aiven.env; \
	if [ -f "$$f" ]; then \
		h=$$(sed -n 's/^host=//p' "$$f" | head -1); \
		p=$$(sed -n 's/^port=//p' "$$f" | head -1); \
		d=$$(sed -n 's/^dbname=//p' "$$f" | head -1); \
	fi; \
	SMOKE_SOURCE_HOST="$${SMOKE_SOURCE_HOST:-$$h}" \
	SMOKE_SOURCE_PORT="$${SMOKE_SOURCE_PORT:-$$p}" \
	SMOKE_SOURCE_DB="$${SMOKE_SOURCE_DB:-$$d}" \
	./scripts/smoke.sh

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

.PHONY: measure-lakehouse-schema
measure-lakehouse-schema:  ## Phép đo Task 2 (2c) — kích thước/độ trễ GET .../schema, 200 bảng x 30 cột
	uv run pytest -m benchmark -o addopts="" -s \
		services/loom-query/tests/integration/test_lakehouse_schema_size_benchmark.py

.PHONY: measure-write
measure-write: check-context  ## Rủi ro #4 (CỬA CHẶN GĐ2) — đường ghi PyIceberg trên cụm k3d thật, 50 GB mặc định
	@# Mặc định --target-raw-gb 50 (ngưỡng đã chốt với chủ dự án — xem docstring
	@# của scripts/measure_write_path.py). Kiểm ở quy mô nhỏ:
	@#   make measure-write ARGS="--target-raw-gb 1 --batch-raw-mb 100"
	@# Chạy 50 GB thật SẼ MẤT NHIỀU GIỜ — chạy nền:
	@#   nohup make measure-write > /tmp/measure-write-50gb.out 2>&1 &
	uv run python scripts/measure_write_path.py $(ARGS)

.PHONY: measure-write-cleanup
measure-write-cleanup: check-context  ## Dọn bảng/namespace/warehouse/S3 của lần measure-write đã lưu
	uv run python scripts/measure_write_path.py --cleanup $(ARGS)

.PHONY: probe-read-cost
probe-read-cost:  ## Đo 4 GĐ3a — tách chi phí ĐƯỜNG TRUYỀN khỏi chi phí BIẾN ĐỔI trên đường đọc
	@# Trả lời câu hỏi ĐO 3 để ngỏ: trần ~7,3 MB/s của giai đoạn đọc nguồn là
	@# đường internet tới Aiven, hay là bước `list[dict]` -> Arrow trong
	@# `PostgresConnector._read_rows`? Hai câu trả lời chỉ về hai hướng ngược
	@# nhau (viết lại ngưỡng / sửa connector), nên phải tách chúng bằng số.
	@#
	@# KHÔNG có `check-context`, và đó là chủ ý chứ không phải bỏ sót: phép đo
	@# này KHÔNG chạm cụm k3d. Nó chạy trên HOST, nói chuyện thẳng với Aiven
	@# (tên miền phân giải được từ host — `make migrate` vẫn làm thế) và với
	@# một container Postgres do testcontainers dựng. Đòi context k3d ở đây chỉ
	@# là một hàng rào giả cho một thứ không đi qua cụm.
	@#
	@# CẦN Docker (testcontainers dựng `postgres:17-alpine` cho hai ô "local").
	@# Chỉ đo Aiven, không cần Docker:
	@#   make probe-read-cost ARGS="--sources aiven"
	@#
	@# KHÔNG GHI GÌ VÀO AIVEN, và điều đó được THI HÀNH: connection tới Aiven mở
	@# với `-c default_transaction_read_only=on`, dòng được sinh server-side
	@# bằng `generate_series` (không chạm đĩa, không sinh WAL), và script in
	@# tổng `pg_database_size` TRƯỚC/SAU để con số tự nói. Lý do đầy đủ nằm ở
	@# docstring của script: một lần chạy trước đã đẩy service này sang chỉ-đọc
	@# THẬT trong lúc control plane đang sống.
	@#
	@# Đọc credential từ deploy/local/aiven.env (gitignore) — KHÔNG đi qua dòng
	@# lệnh, không vào log, không vào progress.json.
	uv run python scripts/probe_read_path_cost.py $(ARGS)

.PHONY: probe-read-cost-pod
probe-read-cost-pod: check-context  ## Đo 4b GĐ3a — CÙNG phép đo đó nhưng TỪ TRONG CỤM
	@# Đo 4 chạy trên HOST và để lại ĐÚNG một khoảng trống: trần 11,14 MB/s là
	@# trần của HOST, còn ĐO 3 và pod nạp thật chạy TRONG CỤM. Suy ra từ host cho
	@# một bảng thật là 16,12s; ĐO 3 đo trong cụm 20,4s — ~27% chưa quy được, và
	@# có ĐÚNG hai ứng viên: chi phí chạy trong pod, và chi phí quét bảng thật
	@# (thứ `generate_series` không có). Target này đo ứng viên THỨ NHẤT bằng
	@# cách chạy CÙNG MỘT SCRIPT, cùng câu SQL, cùng hình dạng dòng — chỉ đổi chỗ
	@# đứng. Hiệu hai lần chạy LÀ chi phí pod, không còn lẫn với thứ gì khác.
	@#
	@# CHẠY LẠI `make probe-read-cost` (host) NGAY TRƯỚC HOẶC SAU, đừng so với số
	@# đã in trong báo cáo cũ: băng thông internet trôi theo giờ (Đo 4 đã đo RTT
	@# 34,5 ms; một lần đo lại hôm sau ra 46 ms). So một lần chạy trong cụm HÔM
	@# NAY với một lần chạy trên host HÔM QUA thì hiệu thu được là "pod + trôi
	@# mạng", và không tách được hai thứ đó nữa — đúng cái lỗi mà target này sinh
	@# ra để sửa.
	@#
	@# Image lấy TỪ `LOOM_TASK_IMAGE` của deployment loom-api, không viết cứng —
	@# cùng lý do đã ghi ở `measure-ingest`: đó là đúng image mà `JobLauncher`
	@# dựng pod nạp thật bằng, nên phép đo chạy trên cùng bộ psycopg/pyarrow mà
	@# production chạy. Một image khác đo một hệ thống khác.
	@#
	@# `requests.cpu=50m` khớp `Settings.task_cpu` của pod nạp thật, và KHÔNG có
	@# `limits.cpu` — vì `JobLauncher.launch` cũng không đặt (xem
	@# `V1ResourceRequirements(requests={"cpu":..., "memory":...},
	@# limits={"memory":...})`). Đặt một `limits.cpu` ở đây sẽ đo một pod bị bóp
	@# CPU mà production không bị, tức là đo một hệ thống khác.
	@#
	@# KHÔNG đặt `limits.memory` dù pod nạp thật có: cùng quyết định đã ghi ở
	@# `measure-ingest` — một phép đo bị OOMKilled không cho ra số nào, nó chỉ
	@# cho ra một pod chết. Ô `connector` ở `batch_rows=100.000` đã đo RSS đỉnh
	@# 451 MiB trên host, sát 512Mi; để nguyên limit là tự đặt một cửa sập giữa
	@# phép đo. RSS vẫn đọc được ở dòng "RSS đỉnh" của tổng kết.
	@#
	@# Credential: Secret `loom-db-app` MOUNT THÀNH THƯ MỤC (`/aiven`), không qua
	@# biến môi trường và không qua dòng lệnh. Hai lý do: (a) khoá `ca.pem` KHÔNG
	@# phải một tên biến môi trường hợp lệ nên `envFrom` sẽ bỏ qua nó và ghi
	@# `InvalidVariableNames` (đúng cái bẫy đã ghi ở `infra-local-source-secret`),
	@# mà `sslmode=verify-full` thì cần chính file đó; (b) giá trị không lộ trong
	@# `kubectl describe pod`. Script đọc thư mục này qua `--aiven-secret-dir`
	@# (xem `read_secret_dir`).
	@#
	@# `PYTHONUNBUFFERED=1`: `Logger.line` in bằng `print`, và stdout của pod là
	@# một PIPE chứ không phải tty nên Python gom khối 8 KB. Không có biến này,
	@# một pod chết giữa chừng (OOM, timeout, mạng đứt) mang theo mọi dòng chưa
	@# kịp xả — mất cả những mẫu ĐÃ ĐO XONG. Có nó, `kubectl logs` là bản sao
	@# bền của từng mẫu ngay khi mẫu đó xong, và `progress.json` trong `/tmp` của
	@# pod (chết theo pod) không còn là bản duy nhất.
	@#
	@# `--verify-read-only`: thử CỐ Ý `CREATE TEMP TABLE`/`CREATE TABLE` TRƯỚC
	@# khi đo và đòi server TỪ CHỐI cả hai. Ràng buộc "không ghi gì vào Aiven"
	@# phải được chứng minh TỪ TRONG CỤM chứ không suy ra từ lần chạy trên host:
	@# đây là một connection khác, mở từ một chỗ khác.
	@#
	@# Nạp script qua ConfigMap và `backoffLimit=0`: cùng hai lý do đã ghi dài ở
	@# `measure-ingest-pod` (backtick trong docstring làm `python -c "$$(cat ...)"`
	@# vỡ thật; thiếu `backoffLimit` thì Kubernetes tự chạy lại một job hỏng).
	@#
	@# Chờ TRẠNG THÁI CUỐI CÙNG (`.status.succeeded`/`.status.failed`) thay vì
	@# `kubectl wait --for=condition=complete`: script thoát khác 0 khi hàng rào
	@# chỉ-đọc hỏng, và với `backoffLimit=0` một Job như thế KHÔNG BAO GIỜ đạt
	@# Complete — `kubectl wait` sẽ treo tới hết timeout rồi báo một lỗi chung
	@# chung thay vì in log cho biết chuyện gì đã xảy ra.
	@set -eo pipefail; \
	img=$$(kubectl -n $(NS) get deploy loom-api \
	  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LOOM_TASK_IMAGE")].value}'); \
	test -n "$$img" || { echo "không đọc được LOOM_TASK_IMAGE từ deploy/loom-api"; exit 1; }; \
	echo "image: $$img"; \
	kubectl -n $(NS) delete job probe-read-cost --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-read-cost-script --ignore-not-found; \
	kubectl -n $(NS) create configmap probe-read-cost-script \
	  --from-file=probe_read_path_cost.py=scripts/probe_read_path_cost.py \
	  --from-file=_aiven_guard.py=scripts/_aiven_guard.py \
	  --dry-run=client -o yaml | kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) create job probe-read-cost --image="$$img" --dry-run=client -o json \
	  -- python /scripts/probe_read_path_cost.py --sources aiven --verify-read-only \
	     --aiven-secret-dir /aiven --aiven-ca /aiven/ca.pem --state-dir /tmp/probe-read-cost \
	     $(ARGS) \
	| jq '.spec.backoffLimit = 0 | .spec.template.spec.containers[0].volumeMounts = [{"name":"script","mountPath":"/scripts"},{"name":"aiven","mountPath":"/aiven","readOnly":true}] | .spec.template.spec.volumes = [{"name":"script","configMap":{"name":"probe-read-cost-script"}},{"name":"aiven","secret":{"secretName":"loom-db-app"}}] | .spec.template.spec.containers[0].env = [{"name":"PYTHONUNBUFFERED","value":"1"}] | .spec.template.spec.containers[0].resources = {"requests":{"cpu":"50m","memory":"512Mi"}}' \
	| kubectl -n $(NS) apply -f -; \
	elapsed=0; phase=""; \
	while [ "$$phase" != "Complete" ] && [ "$$phase" != "Failed" ]; do \
	  if [ "$$elapsed" -ge 2400 ]; then \
	    echo "Job không kết thúc sau 2400s — trạng thái:"; \
	    kubectl -n $(NS) describe job probe-read-cost | tail -20; \
	    kubectl -n $(NS) logs job/probe-read-cost --tail=40 || true; \
	    kubectl -n $(NS) delete job probe-read-cost --ignore-not-found; \
	    kubectl -n $(NS) delete configmap probe-read-cost-script --ignore-not-found; \
	    exit 1; \
	  fi; \
	  sleep 5; elapsed=$$((elapsed + 5)); \
	  succeeded=$$(kubectl -n $(NS) get job probe-read-cost \
	    -o jsonpath='{.status.succeeded}' 2>/dev/null || true); \
	  failed=$$(kubectl -n $(NS) get job probe-read-cost \
	    -o jsonpath='{.status.failed}' 2>/dev/null || true); \
	  if [ "$$succeeded" = "1" ]; then phase="Complete"; \
	  elif [ "$$failed" = "1" ]; then phase="Failed"; \
	  else phase=""; fi; \
	done; \
	kubectl -n $(NS) logs job/probe-read-cost; \
	kubectl -n $(NS) delete job probe-read-cost --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-read-cost-script --ignore-not-found; \
	test "$$phase" = "Complete"

.PHONY: measure-ingest-pod
measure-ingest-pod: check-context  ## Đo 1 GĐ3a (CỬA CHẶN) — RAM ghi Iceberg từ TRONG cụm
	@# Dùng image loom-query đang chạy: nó đã có pyiceberg/pyarrow/icebergkit. Dựng
	@# một image riêng để đo cái có thể làm ta bỏ cả hướng đi là làm ngược thứ tự.
	@#
	@# Nạp script qua ConfigMap, KHÔNG qua `python -c "$$(cat ...)"`: docstring của
	@# measure_ingest_pod.py dùng dấu backtick (`) để đánh dấu tên biến/lệnh — bên
	@# trong double-quote của shell, backtick kích hoạt command substitution, nên
	@# `"$$(cat scripts/measure_ingest_pod.py)"` khiến bash cố CHẠY "sys.getsizeof"
	@# như một lệnh thay vì giữ nó là văn bản. Đã thử thật trên máy này: vỡ đúng
	@# như dự đoán ("sys.getsizeof: command not found"). ConfigMap chép nguyên byte
	@# của file, không qua diễn giải shell nào.
	@#
	@# Cũng KHÔNG dùng heredoc (`kubectl apply -f - <<EOF ... EOF`) để dựng Job từ
	@# YAML nhiều dòng: đã thử thật trên máy này — mỗi dòng vật lý trong recipe
	@# của Make chạy ở MỘT shell invocation RIÊNG trừ khi dòng đó kết thúc bằng
	@# `\`, nên nội dung heredoc (không có `\` cuối dòng, vì đó là YAML thật) bị
	@# tách khỏi lệnh `cat <<EOF` sinh ra nó và chạy như các LỆNH SHELL độc lập —
	@# "line one" bị in nhầm, rồi "make: line: No such file or directory". Thay
	@# vào đó: `kubectl create job --dry-run=client -o json` sinh Job JSON hợp lệ,
	@# `jq` (đã là dependency của target `cluster-up`/`infra-local-secret`) chèn
	@# thêm volume/volumeMount, rồi pipe thẳng vào `kubectl apply -f -` — toàn bộ
	@# nằm trên MỘT lệnh pipe nối bằng `\`, không có JSON/YAML nào tách dòng.
	@#
	@# `set -eo pipefail`: BẮT BUỘC theo đúng lý do đã ghi ở `helm-validate` (mục
	@# "set -e là BẮT BUỘC" phía trên trong file này). Không có nó, `img=$$(...)`
	@# rỗng khi `kubectl get deploy` lỗi vẫn cho recipe chạy tiếp với
	@# `--image=""`, và `kubectl create job ... | jq ... | kubectl apply` chỉ báo
	@# exit status của lệnh CUỐI trong pipe — một `jq` hỏng biến mất, lộ ra sau
	@# 600s chờ `kubectl wait` timeout bằng một thông báo chung chung thay vì lỗi
	@# tức thời và cụ thể.
	@#
	@# `jq` còn tiêm `env`: script SỬA SAU REVIEW ghi Iceberg THẬT (xem docstring
	@# của measure_ingest_pod.py) nên cần credential GỐC của MinIO để Lakekeeper
	@# tự AssumeRole hộ lúc tạo warehouse. Lấy qua `secretKeyRef` từ Secret
	@# `minio-root` (khoá `root-user`/`root-password`) — KHÔNG `kubectl get
	@# secret` rồi truyền qua biến môi trường của `kubectl create job` như
	@# `scripts/measure_write_path.py` làm trên host: pod đọc thẳng Secret của
	@# chính cụm nó đang chạy trong, không cần lôi giá trị ra ngoài rồi nhét lại.
	@#
	@# `jq` còn ép `backoffLimit=0`: mặc định Job của Kubernetes là 6 — một lần
	@# chạy thật ĐÃ DÍNH đúng bẫy này. Script lần đầu ghi Iceberg thật ném
	@# `AttributeError` ở bước dọn (xem SỬA SAU REVIEW trong measure_ingest_pod.py),
	@# và vì không có `backoffLimit: 0`, Kubernetes tự tạo pod MỚI thử lại — pod
	@# thứ hai ghi 20 lô Iceberg khác, thất bại THEO ĐÚNG CÁCH CŨ, và để lại HAI
	@# warehouse rác trên Lakekeeper thay vì một. Dọn tay xong mới thêm dòng này.
	@#
	@# ĐÃ ĐO (ghi Iceberg THẬT: bootstrap + warehouse + namespace + bảng +
	@# create_from/append + commit + dọn — --rows-per-batch 200000 mặc định,
	@# --batches 20 mặc định, 4 triệu dòng): rss_peak_mib=406, và KHÔNG PHẲNG —
	@# leo ĐỀU qua từng lô (284 → 326 → 343 → 355 → 365 → 374 → 381 → 388 → 399
	@# → 406 MiB, không có lô nào tụt xuống). Đây là NGƯỢC LẠI với bản đo đầu
	@# (chỉ sinh Arrow batch, không chạm Iceberg): bản đó phẳng ở 235 MiB vì
	@# không có gì tích luỹ qua các lô. Bản ghi thật này CÓ — nghi nhất là
	@# `RestCatalog`/`Table` giữ lại sổ sách snapshot/manifest ngày càng dài
	@# qua mỗi `load_table()`+commit, hoặc buffer của client S3 không được giải
	@# phóng giữa các lần ghi. CHƯA xác định được chỗ rò cụ thể — nằm ngoài
	@# phạm vi Đo 1 (chỉ đo, không sửa PyIceberg/PyArrow).
	@#
	@# `make ram` chạy TRONG LÚC Job còn sống (lô ~18-19): CẢ NODE 1501 Mi /
	@# trần 1843 Mi (còn dư 342 Mi ở THỜI ĐIỂM ĐÓ — nhưng RSS của pod vẫn đang
	@# leo, xem đoạn trên, nên con số này sẽ còn giảm nếu Job chạy lâu hơn).
	@# cgroup `memory.current` của riêng pod lúc đó: 290 Mi, thấp hơn đỉnh cuối
	@# cùng 406 MiB vì đọc TRƯỚC khi lô cuối chạy xong — không mâu thuẫn với
	@# lý do `ru_maxrss` mới là con số phải tin (xem docstring measure_ingest_pod.py).
	@#
	@# NGƯỠNG: 406 MiB > 340 Mi. **BLOCKED.** Đường ghi Iceberg từ trong pod
	@# KHÔNG vừa ngân sách RAM đã chốt trước khi đo. KHÔNG tự ý hạ
	@# --rows-per-batch để "qua" ngưỡng — đó là việc của quyết định thiết kế
	@# tiếp theo (spec Giai đoạn 3a mục 8 đã liệt sẵn ba lối ra, theo thứ tự
	@# nên dùng: hạ số dòng/lô, chuyển MinIO ra VPS, nâng trần k3d), không phải
	@# việc của phép đo này. Vì RSS còn đang LEO chứ chưa ổn định ở lô 20, một
	@# lần chạy dài hơn (nhiều lô hơn) rất có thể còn cao hơn 406 MiB nữa — số
	@# đo được ở đây là CẬN DƯỚI của chi phí thật, không phải đỉnh tuyệt đối.
	@set -eo pipefail; \
	img=$$(kubectl -n $(NS) get deploy loom-query -o jsonpath='{.spec.template.spec.containers[0].image}'); \
	echo "image: $$img"; \
	kubectl -n $(NS) delete job measure-ingest --ignore-not-found; \
	kubectl -n $(NS) delete configmap measure-ingest-script --ignore-not-found; \
	kubectl -n $(NS) create configmap measure-ingest-script \
	  --from-file=measure_ingest_pod.py=scripts/measure_ingest_pod.py \
	  --dry-run=client -o yaml | kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) create job measure-ingest --image="$$img" --dry-run=client -o json \
	  -- python /scripts/measure_ingest_pod.py $(ARGS) \
	| jq '.spec.backoffLimit = 0 | .spec.template.spec.containers[0].volumeMounts = [{"name":"script","mountPath":"/scripts"}] | .spec.template.spec.volumes = [{"name":"script","configMap":{"name":"measure-ingest-script"}}] | .spec.template.spec.containers[0].env = [{"name":"MINIO_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-user"}}},{"name":"MINIO_SECRET_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-password"}}}]' \
	| kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) wait --for=condition=complete job/measure-ingest --timeout=600s \
	  || { echo "Job KHÔNG hoàn tất — xem trạng thái dưới:"; \
	       kubectl -n $(NS) describe job measure-ingest | tail -20; exit 1; }; \
	kubectl -n $(NS) logs job/measure-ingest | tail -10; \
	kubectl -n $(NS) delete job measure-ingest --ignore-not-found; \
	kubectl -n $(NS) delete configmap measure-ingest-script --ignore-not-found

.PHONY: probe-single-commit
probe-single-commit: check-context  ## Đo 2 GĐ3a (CỬA CHẶN) — PyIceberg commit một lần được không
	@# Cùng lý do và cùng cách dựng Job như measure-ingest-pod ngay trên: dùng
	@# image loom-query đang chạy (đã có pyiceberg/pyarrow), nạp script qua
	@# ConfigMap (KHÔNG heredoc/`python -c`, xem chú thích dài ở measure-ingest-pod
	@# cho lý do cả hai cách đó đều vỡ thật), tiêm credential MinIO GỐC qua
	@# secretKeyRef, và `backoffLimit=0` — thiếu cờ này Kubernetes tự thử lại
	@# job hỏng và để lại NHIỀU warehouse rác trên Lakekeeper, đúng bẫy đã ăn ở
	@# Đo 1.
	@#
	@# `set -eo pipefail`: BẮT BUỘC theo đúng lý do đã ghi ở `helm-validate` và
	@# `measure-ingest-pod` phía trên — không có nó, một `jq` hỏng giữa pipe
	@# biến mất và chỉ lộ ra sau khi chờ hết vòng lặp bên dưới bằng một lỗi
	@# chung chung thay vì tức thời.
	@#
	@# KHÔNG dùng `kubectl wait --for=condition=complete` như measure-ingest-pod:
	@# script này CHỦ ĐỘNG thoát mã khác 0 khi verdict KHÔNG ĐẠT (yêu cầu Đo 2
	@# GĐ3a — một phép đo chỉ báo hỏng qua chữ, không qua mã thoát, sớm muộn sẽ
	@# bị một thứ chỉ đọc mã thoát chạy nhầm). Với `backoffLimit=0`, pod thoát
	@# khác 0 đưa Job sang điều kiện Failed — KHÔNG BAO GIỜ đạt Complete, dù
	@# phép đo đã chạy xong và in đủ kết quả. Vòng lặp dưới chờ TRẠNG THÁI CUỐI
	@# CÙNG bất kể Complete hay Failed, rồi mới đọc log/dọn Job.
	@#
	@# Đọc TRỰC TIẾP `.status.succeeded`/`.status.failed` (số đếm nguyên),
	@# KHÔNG đọc `.status.conditions[?(@.status=="True")].type` như bản đầu —
	@# ĐÃ VỠ THẬT trên k3s 1.32.13 (server của cụm này): một Job Complete có
	@# HAI condition cùng `status: "True"` (`SuccessCriteriaMet` VÀ `Complete`
	@# — `SuccessCriteriaMet` là condition mới từ tính năng JobSuccessPolicy),
	@# nên jsonpath đó trả về CHUỖI "SuccessCriteriaMet Complete", không bao
	@# giờ khớp `"Complete"` — vòng lặp chờ tới hết 600s một cách vô ích dù Job
	@# đã xong từ ~150 giây, rồi báo "không kết thúc" SAI cho một lần chạy đã
	@# ĐẠT. `.status.succeeded`/`.status.failed` là số đếm đơn giản, ổn định
	@# qua các bản k8s, và với `backoffLimit=0` chỉ có đúng một lần thử nên
	@# luôn là "0" hoặc "1" — không có điều kiện phụ nào cần khớp chuỗi.
	@set -eo pipefail; \
	img=$$(kubectl -n $(NS) get deploy loom-query -o jsonpath='{.spec.template.spec.containers[0].image}'); \
	echo "image: $$img"; \
	kubectl -n $(NS) delete job probe-single-commit --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-single-commit-script --ignore-not-found; \
	kubectl -n $(NS) create configmap probe-single-commit-script \
	  --from-file=probe_iceberg_single_commit.py=scripts/probe_iceberg_single_commit.py \
	  --dry-run=client -o yaml | kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) create job probe-single-commit --image="$$img" --dry-run=client -o json \
	  -- python /scripts/probe_iceberg_single_commit.py $(ARGS) \
	| jq '.spec.backoffLimit = 0 | .spec.template.spec.containers[0].volumeMounts = [{"name":"script","mountPath":"/scripts"}] | .spec.template.spec.volumes = [{"name":"script","configMap":{"name":"probe-single-commit-script"}}] | .spec.template.spec.containers[0].env = [{"name":"MINIO_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-user"}}},{"name":"MINIO_SECRET_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-password"}}}]' \
	| kubectl -n $(NS) apply -f -; \
	elapsed=0; phase=""; \
	while [ "$$phase" != "Complete" ] && [ "$$phase" != "Failed" ]; do \
	  if [ "$$elapsed" -ge 600 ]; then \
	    echo "Job không kết thúc (Complete/Failed) sau 600s — xem trạng thái dưới:"; \
	    kubectl -n $(NS) describe job probe-single-commit | tail -20; \
	    kubectl -n $(NS) delete job probe-single-commit --ignore-not-found; \
	    kubectl -n $(NS) delete configmap probe-single-commit-script --ignore-not-found; \
	    exit 1; \
	  fi; \
	  sleep 5; elapsed=$$((elapsed + 5)); \
	  succeeded=$$(kubectl -n $(NS) get job probe-single-commit \
	    -o jsonpath='{.status.succeeded}' 2>/dev/null || true); \
	  failed=$$(kubectl -n $(NS) get job probe-single-commit \
	    -o jsonpath='{.status.failed}' 2>/dev/null || true); \
	  if [ "$$succeeded" = "1" ]; then phase="Complete"; \
	  elif [ "$$failed" = "1" ]; then phase="Failed"; \
	  else phase=""; fi; \
	done; \
	kubectl -n $(NS) logs job/probe-single-commit; \
	kubectl -n $(NS) delete job probe-single-commit --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-single-commit-script --ignore-not-found; \
	test "$$phase" = "Complete"
	@#
	@# ĐÃ ĐO thật, hai vòng (cụm k3d-loom, mặc định --snapshot-probe-rows=1000,
	@# --rows-per-batch=200000, --batches-c=20):
	@#
	@# VÒNG 1 — A/B/C (2026-08-11), câu hỏi "transaction gộp nhiều lô thành một
	@# snapshot?": KHÔNG ĐẠT.
	@#   A: hai append trong một transaction       -> 2 snapshot
	@#   B: overwrite+append trong một transaction -> 3 snapshot
	@#   C: RSS đỉnh với 20 lô trong một transaction: 499 MiB (bò lên đều,
	@#      338 -> 499 MiB, không có lô nào tụt) — TỆ HƠN cách ghi-commit-từng-lô
	@#      mà Đo 1 đã đo cho ĐÚNG hình dạng này: 406 MiB (mục measure-ingest-pod
	@#      phía trên, dòng "rss_peak_mib=406"). Đọc mã nguồn
	@#      `pyiceberg/table/update/snapshot.py` giải thích đúng con số: mỗi
	@#      `tx.append`/`tx.overwrite` dựng một `_SnapshotProducer` riêng,
	@#      `__exit__` của nó gọi `commit()` xếp một `AddSnapshotUpdate` (một
	@#      `Snapshot` với `snapshot_id` MỚI) vào `Transaction._updates` NGAY,
	@#      trước khi có request mạng nào — hai `tx.append` => 2
	@#      `AddSnapshotUpdate` (khớp A); `tx.overwrite` tự nó là delete (bảng B
	@#      có dữ liệu cũ nên `files_affected=True`, sinh 1 snapshot DELETE) +
	@#      append dữ liệu mới (1 snapshot nữa) = 2, cộng `tx.append` thứ hai =
	@#      3 (khớp B). `commit_transaction()` chỉ gửi MỘT request PUT mang
	@#      TOÀN BỘ `AddSnapshotUpdate` đã xếp — "một transaction" ở PyIceberg
	@#      0.11.1 đảm bảo MỘT request PUT/MỘT điều kiện tranh chấp lạc quan
	@#      (đủ để bảng CŨ đứng nguyên nếu crash trước commit), NHƯNG KHÔNG đảm
	@#      bảo MỘT snapshot Iceberg — đúng giả định mà thiết kế `full` một-
	@#      transaction (Task 12 bản gốc) cần và không giữ được. Thiết kế đó bị
	@#      BÁC BỎ; chủ dự án chọn hướng thay thế: bảng tạm rồi tráo tên qua
	@#      `rename_table` — xem VÒNG 2.
	@#
	@# VÒNG 2 — D (2026-08-12/13), câu hỏi "rename_table dựng nổi một chuỗi
	@# tráo bảng?": ĐẠT (có điều kiện) — ĐÂY LÀ CỬA CHẶN CÒN HIỆU LỰC, A/B/C ở
	@# trên chỉ còn là hồ sơ.
	@#   D1 rename chạy được   : True
	@#   D2 dữ liệu nguyên vẹn : True  (1000/1000 dòng khớp CẢ id lẫn pad)
	@#   D3 tên cũ biến mất    : True  (NoSuchTableError — đúng là MOVE)
	@#   D4 rename đè tên cũ   : conflict  (TableAlreadyExistsError — TỪ CHỐI)
	@#
	@#   D1-D3 ĐẠT: `rename_table` là một nguyên liệu THẬT dùng được (chạy
	@#   được qua Lakekeeper, giữ nguyên dữ liệu, và là MOVE thật — tên cũ mất
	@#   hẳn). D4 (không quyết định mã thoát, nhưng quyết định CHUỖI THAO TÁC):
	@#   rename TỪ CHỐI ghi đè lên một tên đã tồn tại. Cộng D3 (MOVE) với D4 (từ
	@#   chối ghi đè): chuỗi tráo bảng của `full` KHÔNG THỂ là một lời gọi
	@#   `rename` duy nhất — phải qua NHIỀU bước catalog: HOẶC `drop(target)`
	@#   rồi `rename(staging -> target)` (cửa sổ: bảng KHÔNG TỒN TẠI), HOẶC ba
	@#   bước `rename(target -> target_old)` + `rename(staging -> target)` +
	@#   `drop(target_old)` (cửa sổ hẹp hơn: giữa hai lời gọi catalog nhanh).
	@#   Dù chọn chuỗi nào, có một CỬA SỔ mà tên target KHÔNG PHÂN GIẢI được —
	@#   **`full` chỉ GẦN nguyên tử, KHÔNG nguyên tử tuyệt đối**, và spec Giai
	@#   đoạn 3a KHÔNG được hứa một đảm bảo mà nó không có. Chọn GIỮA hai chuỗi
	@#   thay thế (2 bước hay 3 bước) là quyết định của chủ dự án, không phải
	@#   của phép đo này.
	@#
	@# CẠM BẪY DỌN DẸP RIÊNG CỦA D (đã ăn thật, đã sửa): D1 đổi tên
	@# `probe_d_src` thành `probe_d_dst`, nên dọn dẹp theo một danh sách TÊN ĐÃ
	@# TẠO sẽ cố `drop_table` một tên KHÔNG CÒN TỒN TẠI. Lakekeeper trả 403
	@# Forbidden (không phải 404) cho trường hợp đó — quản trị API của nó CỐ Ý
	@# không phân biệt "không tìm thấy" với "không được phép" cho một principal
	@# ẩn danh — nên PyIceberg ném `ForbiddenError`, KHÔNG phải
	@# `NoSuchTableError`, và bản dọn dẹp cũ (chỉ suppress NoSuchTableError)
	@# không bắt được nó: một lần chạy có VERDICT ĐÚNG (D in đủ bốn câu trả
	@# lời) vẫn thoát mã khác 0 vì dọn dẹp ném lỗi SAU KHI verdict đã chốt —
	@# một false negative. `scripts/probe_iceberg_single_commit.py` sửa bằng
	@# liệt kê THẬT qua `catalog.list_tables` lúc dọn (không theo tên đã tạo)
	@# và bọc MỌI bước dọn dẹp trong try/except riêng — dọn dẹp là best-effort
	@# chạy SAU KHI verdict đã chốt, một lỗi dọn dẹp (dù là gì) không được phép
	@# đổi mã thoát của một phép đo đã tính đúng.

.PHONY: probe-add-files
probe-add-files: check-context  ## Thăm dò GĐ3a — add_files có hạ N file vào MỘT snapshot không
	@# Câu hỏi mà ĐO 2 KHÔNG hỏi. ĐO 2 đo `table.transaction()` + `append` và
	@# thấy nó không gộp snapshot; nó chưa bao giờ chạm `Table.add_files()`, API
	@# đăng ký những file Parquet ĐÃ ghi xong. ĐO 3 định giá khoảng trống đó:
	@# commit catalog là 44,0% thời gian của đường nạp, một hằng số ~0,83s MỖI
	@# LÔ trải trên 2,98 MB. Xem docstring `scripts/probe_iceberg_add_files.py`.
	@#
	@# Cùng cách dựng Job như `probe-single-commit` ngay trên, cùng lý do: image
	@# loom-query đang chạy (đã có pyiceberg/pyarrow/icebergkit), script nạp qua
	@# ConfigMap (KHÔNG `python -c "$$(cat ...)"` — backtick trong docstring làm
	@# bash chạy nội dung như lệnh; KHÔNG heredoc — mỗi dòng recipe của Make là
	@# một shell riêng; cả hai đã vỡ THẬT trên máy này), credential MinIO GỐC qua
	@# secretKeyRef, và `backoffLimit=0` để một lần chạy hỏng không sinh ra
	@# warehouse rác thứ hai.
	@#
	@# Chờ TRẠNG THÁI CUỐI CÙNG bằng `.status.succeeded`/`.status.failed` chứ
	@# KHÔNG `kubectl wait --for=condition=complete`: script thoát khác 0 khi Q1
	@# không đạt, và với `backoffLimit=0` một Job như thế KHÔNG BAO GIỜ đạt
	@# Complete. (Cũng KHÔNG đọc `.status.conditions[...]` — trên k3s 1.32.13 của
	@# cụm này một Job Complete có HAI condition `status: "True"`, xem chú thích
	@# dài ở `probe-single-commit`.)
	@#
	@# Trần 900s, rộng hơn 600s của `probe-single-commit`: phép này chạy 50 lần
	@# commit `append` thật cho Q3 (ĐO 3 đo 42,1s riêng phần commit ở đúng hình
	@# dạng đó) CỘNG một lượt ghi 50 file Parquet, rồi mới tới Q1/Q4/Q5.
	@set -eo pipefail; \
	img=$$(kubectl -n $(NS) get deploy loom-query -o jsonpath='{.spec.template.spec.containers[0].image}'); \
	echo "image: $$img"; \
	kubectl -n $(NS) delete job probe-add-files --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-add-files-script --ignore-not-found; \
	kubectl -n $(NS) create configmap probe-add-files-script \
	  --from-file=probe_iceberg_add_files.py=scripts/probe_iceberg_add_files.py \
	  --dry-run=client -o yaml | kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) create job probe-add-files --image="$$img" --dry-run=client -o json \
	  -- python /scripts/probe_iceberg_add_files.py $(ARGS) \
	| jq '.spec.backoffLimit = 0 | .spec.template.spec.containers[0].volumeMounts = [{"name":"script","mountPath":"/scripts"}] | .spec.template.spec.volumes = [{"name":"script","configMap":{"name":"probe-add-files-script"}}] | .spec.template.spec.containers[0].env = [{"name":"MINIO_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-user"}}},{"name":"MINIO_SECRET_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-password"}}}]' \
	| kubectl -n $(NS) apply -f -; \
	elapsed=0; phase=""; \
	while [ "$$phase" != "Complete" ] && [ "$$phase" != "Failed" ]; do \
	  if [ "$$elapsed" -ge 900 ]; then \
	    echo "Job không kết thúc (Complete/Failed) sau 900s — xem trạng thái dưới:"; \
	    kubectl -n $(NS) describe job probe-add-files | tail -20; \
	    kubectl -n $(NS) logs job/probe-add-files --tail=40 || true; \
	    kubectl -n $(NS) delete job probe-add-files --ignore-not-found; \
	    kubectl -n $(NS) delete configmap probe-add-files-script --ignore-not-found; \
	    exit 1; \
	  fi; \
	  sleep 5; elapsed=$$((elapsed + 5)); \
	  succeeded=$$(kubectl -n $(NS) get job probe-add-files \
	    -o jsonpath='{.status.succeeded}' 2>/dev/null || true); \
	  failed=$$(kubectl -n $(NS) get job probe-add-files \
	    -o jsonpath='{.status.failed}' 2>/dev/null || true); \
	  if [ "$$succeeded" = "1" ]; then phase="Complete"; \
	  elif [ "$$failed" = "1" ]; then phase="Failed"; \
	  else phase=""; fi; \
	done; \
	kubectl -n $(NS) logs job/probe-add-files; \
	kubectl -n $(NS) delete job probe-add-files --ignore-not-found; \
	kubectl -n $(NS) delete configmap probe-add-files-script --ignore-not-found; \
	test "$$phase" = "Complete"
	@#
	@# ĐÃ ĐO thật (2026-08-14, cụm k3d-loom, image loom/query:dev, PyIceberg
	@# 0.11.1; mặc định --rows-per-batch=10000 --batches=50 = 500.000 dòng ~
	@# 0,149 GB, đúng hình dạng C1 của ĐO 3 để so trực tiếp).
	@#
	@# Q1 (CỬA CHẶN) — N file Parquet qua MỘT add_files: **1 snapshot ở CẢ BA**
	@#   N=1 -> 1 snapshot (0,56s);  N=5 -> 1 (0,62s);  N=20 -> 1 (0,61s)
	@#   ĐẠT. Số snapshot KHÔNG đi theo N, và thời gian cũng gần như không đổi
	@#   theo N — `add_files` là MỘT commit catalog bất kể có bao nhiêu file.
	@#   Đây đúng là thứ `transaction()` + `append` KHÔNG làm được ở ĐO 2 (2
	@#   append = 2 snapshot), nên nút thắt "một commit mỗi lô" là ràng buộc của
	@#   CÁCH GỌI thư viện, không phải của PyIceberg 0.11.1.
	@#
	@# Q2 RSS đỉnh (`ru_maxrss`, MỖI đường một tiến trình con riêng — xem
	@# docstring script cho lý do phải fork; nền lúc fork ~99 MiB cho cả hai):
	@#   append từng lô : 281 MiB  (182 MiB trên nền)
	@#   add_files      : 173 MiB  ( 74 MiB trên nền)   -108 MiB
	@#   Rẻ HƠN, không phải đắt hơn: đường add_files giữ đúng một lô sống rồi
	@#   đẩy thẳng ra Parquet, còn đường append cõng thêm sổ sách snapshot/
	@#   manifest tích luỹ qua 50 lần commit (cùng chỗ rò mà ĐO 1 đã thấy RSS
	@#   bò lên đều qua từng lô mà không quy được trách nhiệm).
	@#
	@# Q3 thời gian tường, CÙNG 500.000 dòng:
	@#   append   : 47,9s / 50 commit
	@#   add_files:  3,2s / 1 commit  (ghi Parquet 2,5s + commit 0,7s) — 15x
	@#   Phép kiểm chéo với ĐO 3: 47,9 - 2,5 = 45,4s cho 50 lần commit, sát con
	@#   số 42,1s mà ĐO 3 đo RIÊNG cho giai đoạn commit ở đúng hình dạng này.
	@#   Hai phép đo độc lập nói cùng một điều, nên con số này không phải một
	@#   tạo tác của cách đo.
	@#
	@# Q4a field ID / name-mapping: Parquet do pyarrow ghi KHÔNG mang field ID
	@#   (`id=(không có), pad=(không có)`), còn file do chính Iceberg ghi thì CÓ
	@#   (`id=1, pad=2`) — phép đối chứng, nên đây là khác biệt giữa hai đường
	@#   ghi chứ không phải một quan sát lẻ. Bảng KHÔNG có name-mapping trước
	@#   `add_files`; SAU thì CÓ (2 field, nằm ở thuộc tính
	@#   `schema.name-mapping.default`). **PyIceberg 0.11.1 tự đặt nó** — người
	@#   gọi không phải dựng name-mapping, và không có bước nào bị bỏ sót.
	@#
	@# Q4b vị trí file: **phải nằm TRONG location của CHÍNH bảng.** Hai vị trí
	@#   kia đều hỏng, và hỏng vì `ACCESS_DENIED` chứ KHÔNG vì Iceberg từ chối
	@#   đường dẫn: Lakekeeper vend credential STS hẹp theo TỪNG BẢNG, nên
	@#   credential của bảng A không đọc nổi file nằm ngoài location của A —
	@#   kể cả khi file đó nằm trong cùng warehouse. Cùng lý do, credential vend
	@#   cũng không GHI được ra hai chỗ đó. Ràng buộc này là của Lakekeeper,
	@#   không phải của `add_files`.
	@#
	@# Q4c check_duplicate_files: việc mà nó làm là `inspect.data_files()`, tức
	@#   quét MANIFEST (không phải quét từng data file): 0,017s trên bảng 50
	@#   data file / 1 manifest, 0,029s trên bảng 1 data file / 1 manifest —
	@#   rẻ ở hình dạng này vì `add_files` gom cả 50 file vào MỘT manifest.
	@#   Hiệu số `add_files` đầu-cuối (bật 0,787s vs tắt 0,959s) NHỎ HƠN nhiễu
	@#   của một lần commit và có lần ra ÂM; đừng đọc nó như giá của phép kiểm.
	@#   **TẮT thì KHÔNG an toàn:** đăng ký lại cùng một file với kiểm TẮT đưa
	@#   bảng từ 1000 lên 2000 dòng, IM LẶNG. Với kiểm BẬT: `ValueError`.
	@#
	@# Q4d schema lệch: thừa cột -> ỒN (`ValueError: ... contains more columns`);
	@#   sai kiểu -> ỒN (`ValueError: Mismatch in fields`); **THIẾU cột -> IM**:
	@#   `add_files` nhận, và cột thiếu đọc ra TOÀN NULL. Đó là lỗ DUY NHẤT
	@#   trong ba, và nó không kêu — bên gọi phải tự đối chiếu schema trước khi
	@#   đăng ký, đúng như `check_schema` đang làm cho đường `incremental`.
	@#
	@# Q5 ghép với cú tráo ba bước của `full`: ĐƯỢC. staging nạp bằng add_files
	@#   = 1 snapshot; `rename`/`rename`/`drop` chạy hết ba bước; bảng đích đọc
	@#   ra đúng 5000 dòng của staging và KHÔNG sót id nào của bảng cũ. Đường
	@#   dẫn data file KHÔNG đổi sau cú tráo (vẫn nằm dưới thư mục của bảng
	@#   staging cũ) — và đo được rằng bảng đích MANG THEO location của staging
	@#   (`location sau tráo == location staging`: True), nên credential vend
	@#   của nó vẫn phủ đúng chỗ file nằm. Đó là lý do Q4b và Q5 KHÔNG mâu
	@#   thuẫn nhau, và lý do đó phải đọc được ở đây chứ không phải suy ra.
	@#
	@# KHÔNG SUY RA ĐƯỢC TỪ PHÉP NÀY: `add_files` gỡ 44,0% thời gian mà ĐO 3
	@#   quy cho commit catalog, nhưng ĐO 3 cũng đo SÀN của giai đoạn ĐỌC NGUỒN
	@#   ở ~7,3 MB/s — bằng MỘT NỬA ngưỡng 14,7 MB/s, và không tham số nào phá
	@#   được nó. Cắt commit từ 50 xuống 1 là một thắng lợi thật, nhưng nó KHÔNG
	@#   đưa đường nạp qua cửa chặn; nó chỉ dời nút thắt về đúng chỗ ĐO 3 đã chỉ.

.PHONY: measure-ingest
measure-ingest: check-context  ## Đo 3 GĐ3a (CỬA CHẶN cuối) — đường NẠP tách theo giai đoạn
	@# Ba lần gọi, KHÔNG một: dựng nguồn, đo, rồi xoá nguồn.
	@#
	@#   make measure-ingest ARGS="--seed-source"
	@#   make measure-ingest ARGS="--mode incremental --batch-rows 10000 --run-id <uuid>"
	@#   make measure-ingest ARGS="--mode full        --batch-rows 10000 --run-id <uuid>"
	@#   make measure-ingest ARGS="--mode incremental --batch-rows 40000 --run-id <uuid>"
	@#   make measure-ingest ARGS="--drop-source"
	@#
	@# `--run-id` phải là một hàng `ingest_run` CÓ THẬT (tạo bằng một lần nạp
	@# thật qua `POST /api/v1/lakehouses/<id>/ingest`). Thiếu nó thì giai đoạn 5
	@# — báo tiến độ về control plane — KHÔNG được đo, và báo cáo phải nói thế
	@# thay vì để một cột 0,0s trông như "miễn phí".
	@#
	@# Image lấy TỪ `LOOM_TASK_IMAGE` của deployment loom-api, không viết cứng:
	@# đó là đúng image mà `JobLauncher` dựng pod nạp thật bằng, nên phép đo
	@# chạy trên cùng bộ thư viện (psycopg + pyarrow + pyiceberg + icebergkit)
	@# mà production chạy. Một image khác đo một hệ thống khác.
	@#
	@# KHÔNG đặt `resources.limits.memory`, và đó là một quyết định: pod nạp
	@# thật chạy ở `LOOM_TASK_MEMORY` (512Mi hiện tại), nhưng một phép đo bị
	@# OOMKilled không cho ra số nào cả — nó chỉ cho ra một pod chết. Chạy KHÔNG
	@# limit rồi ĐỌC `ru_maxrss` cho biết cấu hình nào vừa 512Mi và cấu hình nào
	@# không; đó là dữ liệu, còn một OOMKill thì không.
	@#
	@# Nạp script qua ConfigMap và `backoffLimit=0`: cùng hai lý do đã ghi dài ở
	@# `measure-ingest-pod` phía trên (backtick trong docstring làm `python -c
	@# "$$(cat ...)"` vỡ thật; thiếu `backoffLimit` thì Kubernetes tự chạy lại
	@# một job hỏng và để lại NHIỀU warehouse rác).
	@#
	@# Chờ TRẠNG THÁI CUỐI CÙNG (`.status.succeeded`/`.status.failed`, số đếm),
	@# KHÔNG `kubectl wait --for=condition=complete`: cùng lý do đã ghi ở
	@# `probe-single-commit` — script này thoát khác 0 khi hỏng, và với
	@# `backoffLimit=0` một Job như thế KHÔNG BAO GIỜ đạt Complete.
	@#
	@# Trần 3600s: một cấu hình 10.000 dòng/lô trên 1,2 triệu dòng là 120 lô, và
	@# 2c đã đo commit catalog ở 6,7s/lô trên một bảng lớn — nếu đường này cũng
	@# thế thì một cấu hình mất trên 13 phút. Trần phải rộng hơn hẳn dự đoán,
	@# nếu không phép đo bị chính cái đồng hồ của nó cắt ngang.
	@set -eo pipefail; \
	img=$$(kubectl -n $(NS) get deploy loom-api \
	  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LOOM_TASK_IMAGE")].value}'); \
	test -n "$$img" || { echo "không đọc được LOOM_TASK_IMAGE từ deploy/loom-api"; exit 1; }; \
	echo "image: $$img"; \
	kubectl -n $(NS) delete job measure-ingest-path --ignore-not-found; \
	kubectl -n $(NS) delete configmap measure-ingest-path-script --ignore-not-found; \
	kubectl -n $(NS) create configmap measure-ingest-path-script \
	  --from-file=measure_ingest_path.py=scripts/measure_ingest_path.py \
	  --from-file=_aiven_guard.py=scripts/_aiven_guard.py \
	  --dry-run=client -o yaml | kubectl -n $(NS) apply -f -; \
	kubectl -n $(NS) create job measure-ingest-path --image="$$img" --dry-run=client -o json \
	  -- python /scripts/measure_ingest_path.py $(ARGS) \
	| jq '.spec.backoffLimit = 0 | .spec.template.spec.containers[0].volumeMounts = [{"name":"script","mountPath":"/scripts"}] | .spec.template.spec.volumes = [{"name":"script","configMap":{"name":"measure-ingest-path-script"}}] | .spec.template.spec.containers[0].env = [{"name":"MINIO_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-user"}}},{"name":"MINIO_SECRET_KEY","valueFrom":{"secretKeyRef":{"name":"minio-root","key":"root-password"}}},{"name":"BENCH_PG_USER","valueFrom":{"secretKeyRef":{"name":"loom-db-app","key":"username"}}},{"name":"BENCH_PG_PASSWORD","valueFrom":{"secretKeyRef":{"name":"loom-db-app","key":"password"}}},{"name":"BENCH_PG_HOST","valueFrom":{"secretKeyRef":{"name":"loom-db-app","key":"host"}}},{"name":"BENCH_PG_PORT","valueFrom":{"secretKeyRef":{"name":"loom-db-app","key":"port"}}},{"name":"BENCH_PG_DBNAME","valueFrom":{"secretKeyRef":{"name":"loom-db-app","key":"dbname"}}},{"name":"LOOM_INGEST_SHARED_SECRET","valueFrom":{"secretKeyRef":{"name":"loom-app","key":"ingest-shared-secret"}}}]' \
	| kubectl -n $(NS) apply -f -; \
	elapsed=0; phase=""; \
	while [ "$$phase" != "Complete" ] && [ "$$phase" != "Failed" ]; do \
	  if [ "$$elapsed" -ge 3600 ]; then \
	    echo "Job không kết thúc sau 3600s — trạng thái:"; \
	    kubectl -n $(NS) describe job measure-ingest-path | tail -20; \
	    kubectl -n $(NS) logs job/measure-ingest-path --tail=40 || true; \
	    kubectl -n $(NS) delete job measure-ingest-path --ignore-not-found; \
	    kubectl -n $(NS) delete configmap measure-ingest-path-script --ignore-not-found; \
	    exit 1; \
	  fi; \
	  sleep 5; elapsed=$$((elapsed + 5)); \
	  succeeded=$$(kubectl -n $(NS) get job measure-ingest-path \
	    -o jsonpath='{.status.succeeded}' 2>/dev/null || true); \
	  failed=$$(kubectl -n $(NS) get job measure-ingest-path \
	    -o jsonpath='{.status.failed}' 2>/dev/null || true); \
	  if [ "$$succeeded" = "1" ]; then phase="Complete"; \
	  elif [ "$$failed" = "1" ]; then phase="Failed"; \
	  else phase=""; fi; \
	done; \
	kubectl -n $(NS) logs job/measure-ingest-path; \
	kubectl -n $(NS) delete job measure-ingest-path --ignore-not-found; \
	kubectl -n $(NS) delete configmap measure-ingest-path-script --ignore-not-found; \
	test "$$phase" = "Complete"

.PHONY: ram
ram: check-context  ## Tổng RAM cụm đang dùng, so với NGÂN SÁCH tự đặt 4 GiB
	@# ĐỌC KỸ CON SỐ NÀY TRƯỚC KHI DỰA VÀO NÓ.
	@#
	@# 4096 Mi là NGÂN SÁCH TỰ ĐẶT, KHÔNG phải một giới hạn được thi hành. Không
	@# có cgroup nào, không có `--memory` nào chặn cụm ở con số đó — vượt qua nó
	@# thì target này đỏ, còn cụm vẫn chạy bình thường.
	@#
	@# Nói rõ vì suốt Giai đoạn 0 tới 2 con số cũ (1843 Mi, "trần 1,8 GB") đã bị
	@# đối xử như định luật vật lý: nó định hình limit 448Mi của MinIO, 384Mi của
	@# loom-query, và suýt biến Giai đoạn 3a thành BLOCKED. Kiểm thật 2026-08-11:
	@#
	@#   docker inspect k3d-loom-server-0 --format '{{ .HostConfig.Memory }}'  -> 0
	@#   grep -i memory deploy/k3d/cluster.yaml                                -> không có
	@#   docker stats k3d-loom-server-0                     -> 1.218GiB / 15.34GiB
	@#
	@# Mẫu số là RAM MÁY (16 GB), không phải 1843 Mi. Con số cũ chỉ tồn tại trong
	@# đúng cái printf ở cuối target này.
	@#
	@# Vậy vì sao vẫn giữ một ngân sách? Vì máy này có sức ép thật: lúc đo, 9,9 GB
	@# đã dùng và swap đã ăn 2/4 GB — không phải do cụm (cụm 1,2 GB), nhưng một cụm
	@# phình không ai để ý sẽ đẩy swap lên và làm chậm mọi thứ. Ngân sách để BẮT
	@# TĂNG TRƯỞNG BẤT THƯỜNG, không phải để mô tả một bức tường.
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
	printf '  CẢ NODE:         %d Mi   ngân sách 4096 Mi (tự đặt, KHÔNG thi hành)\n' "$$node_mib"; \
	printf '  còn dư %d Mi — RAM máy thật là 15,34 GiB, xem giải thích đầu target\n' \
	  $$(( 4096 - node_mib )); \
	if [ "$$node_mib" -gt 4096 ]; then \
	  echo "  VƯỢT NGÂN SÁCH — cụm vẫn chạy, nhưng đây là tăng trưởng cần giải thích."; \
	  echo "  Đừng nâng con số này cho hết đỏ mà chưa biết cái gì đang ăn thêm."; exit 1; fi
