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
