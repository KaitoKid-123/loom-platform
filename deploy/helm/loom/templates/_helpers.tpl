{{- define "loom.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Cố ý bỏ qua .Release.Name: mọi lần install đều ra loom-api/loom-web,
   đúng thứ Tiltfile và bài kiểm thu ở Task 16/17 tham chiếu bằng tên cứng.
   Cái giá là hai release không bao giờ có thể chung sống trong một namespace —
   đừng "sửa" định nghĩa này để hỗ trợ nhiều release mà không tính đến điều đó. */}}
{{- define "loom.fullname" -}}
{{- printf "%s" (include "loom.name" .) -}}
{{- end -}}

{{- define "loom.labels" -}}
app.kubernetes.io/name: {{ include "loom.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "loom.appSecretName" -}}
{{- if .Values.oidc.existingSecret -}}
{{- .Values.oidc.existingSecret -}}
{{- else -}}
{{- printf "%s-app" (include "loom.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Biến môi trường dùng chung cho api và job migration */}}
{{- define "loom.apiEnv" -}}
- name: LOOM_ENVIRONMENT
  value: {{ .Values.app.environment | quote }}
- name: LOOM_LOG_LEVEL
  value: {{ .Values.app.logLevel | quote }}
- name: LOOM_DB_HOST
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.hostKey }}
- name: LOOM_DB_PORT
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.portKey }}
- name: LOOM_DB_NAME
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.nameKey }}
# asyncpg đọc PGSSLROOTCERT (đã kiểm chứng). Không có nó thì verify-full đi tìm
# ~/.postgresql/root.crt và thất bại với thông báo không liên quan tới Aiven.
- name: PGSSLROOTCERT
  value: /etc/loom/db-ca/{{ .Values.database.caKey }}
- name: LOOM_DB_SSLMODE
  value: {{ .Values.database.sslMode | quote }}
- name: LOOM_DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.usernameKey }}
- name: LOOM_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.passwordKey }}
{{- end -}}

{{/* Biến môi trường CHỈ cho container `api` — KHÔNG cho migrate-job.yaml.
   Khoảng trống Giai đoạn 2a phát hiện: tạo item `lakehouse` trước đây chỉ chèn
   một hàng Postgres, không tạo warehouse Lakekeeper nào, nên nó không dùng
   được — xem docstring `loom_api.warehouse_provisioning`.

   Credential ở đây là credential GỐC của MinIO, cùng Secret mà
   `storage.existingSecret` đã khai từ trước (`minio-root`, xem
   `deploy/infra/minio.yaml`) nhưng chưa từng được một Deployment nào đọc tới
   — nợ credential đã ghi ở README, và phép canh AST canh phạm vi đọc nó nằm ở
   `services/api/tests/test_root_credential_guard.py`.

   Tách khỏi `loom.apiEnv` có chủ đích: migrate-job.yaml CHỈ chạy `alembic
   upgrade head`, không hề chạm Lakekeeper — nhét thêm một Secret nó không cần
   vào đó là một cách bó job db-migration vào một sự cố MinIO không liên quan
   gì tới nó. */}}
{{/*
Credential GỐC MinIO + bucket, cho service nào cần gọi STS AssumeRole.

Nhận một dict `{root, prefix}` vì HAI service dùng cùng bộ giá trị này với HAI
tiền tố biến môi trường khác nhau: `loom-api` đọc `LOOM_*` còn `loom-query` đọc
`LOOM_QUERY_*` (xem `env_prefix` trong hai file `config.py`). Chép thành hai bản
là cách chắc chắn để một bên được cập nhật còn bên kia thì không — và lệch ở đây
nghĩa là một service lặng lẽ chạy bằng credential giữ chỗ.

Đã xảy ra thật: `query-deployment.yaml` thiếu hẳn bốn biến này cho tới khi có
người so hai bản render với nhau.
*/}}
{{- define "loom.storageRootEnv" -}}
{{- $prefix := .prefix -}}
{{- with .root -}}
{{- if eq $prefix "LOOM_" }}
{{- /* Chỉ `loom-api` đọc biến này (cấp phát warehouse). `loom-query` nói chuyện
       với catalog qua `LOOM_QUERY_CATALOG_URI` và không có trường tương ứng —
       tiêm vào đó chỉ tạo một biến không ai đọc. */}}
- name: {{ $prefix }}LAKEKEEPER_URL
  value: "http://{{ include "loom.fullname" . }}-lakekeeper.{{ .Release.Namespace }}.svc.cluster.local:8181"
{{- end }}
- name: {{ $prefix }}STORAGE_ENDPOINT
  value: {{ .Values.storage.endpoint | quote }}
- name: {{ $prefix }}STORAGE_BUCKET
  value: {{ .Values.storage.bucket | quote }}
- name: {{ $prefix }}STORAGE_ROOT_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.storage.existingSecret }}
      key: {{ .Values.storage.accessKeyKey }}
- name: {{ $prefix }}STORAGE_ROOT_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.storage.existingSecret }}
      key: {{ .Values.storage.secretKeyKey }}
{{- end -}}
{{- end -}}

{{/* Volume chiếu key CA từ Secret loom-db-app — dùng chung cho api và migrate job */}}
{{- define "loom.dbCaVolume" -}}
- name: db-ca
  secret:
    secretName: {{ .Values.database.existingSecret }}
    items:
      - key: {{ .Values.database.caKey }}
        path: {{ .Values.database.caKey }}
{{- end -}}

{{/* Mount tương ứng, chỉ đọc, tại /etc/loom/db-ca — khớp PGSSLROOTCERT ở trên */}}
{{- define "loom.dbCaVolumeMount" -}}
- name: db-ca
  mountPath: /etc/loom/db-ca
  readOnly: true
{{- end -}}

{{/* Biến môi trường dùng chung cho initContainer `migrate` VÀ container `lakekeeper`
   của lakekeeper-deployment.yaml. Định nghĩa MỘT LẦN ở đây, cố ý: initContainer và
   container phải trỏ vào đúng CÙNG một database, và một helper chung là cách duy
   nhất để hai chỗ không thể lệch nhau khi ai đó sửa một bên mà quên bên kia. Xem
   lakekeeper-deployment.yaml về vì sao migrate là initContainer, không phải Job. */}}
{{- define "loom.lakekeeperEnv" -}}
- name: LAKEKEEPER__PG_HOST_R
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.hostKey }}
- name: LAKEKEEPER__PG_HOST_W
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.hostKey }}
- name: LAKEKEEPER__PG_PORT
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.portKey }}
- name: LAKEKEEPER__PG_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.usernameKey }}
- name: LAKEKEEPER__PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.passwordKey }}
# database RIÊNG của Lakekeeper trên cùng Aiven service — key khác với
# database.nameKey mà api/migrate dùng. Xem values.yaml: database.lakekeeperNameKey.
- name: LAKEKEEPER__PG_DATABASE
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.lakekeeperNameKey }}
# Aiven đi qua Internet công cộng, TLS bắt buộc.
- name: LAKEKEEPER__PG_SSL_MODE
  value: "require"
{{- if .Values.lakekeeper.encryptionSecret }}
- name: LAKEKEEPER__PG_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.lakekeeper.encryptionSecret }}
      key: encryption-key
{{- else }}
- name: LAKEKEEPER__PG_ENCRYPTION_KEY
  value: {{ .Values.lakekeeper.encryptionKeyLocal | quote }}
{{- end }}
- name: LAKEKEEPER__LISTEN_PORT
  value: "8181"
{{- end -}}
