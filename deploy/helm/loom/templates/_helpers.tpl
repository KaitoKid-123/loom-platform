{{- define "loom.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

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
  value: {{ .Values.database.host | quote }}
- name: LOOM_DB_PORT
  value: {{ .Values.database.port | quote }}
- name: LOOM_DB_NAME
  value: {{ .Values.database.name | quote }}
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
