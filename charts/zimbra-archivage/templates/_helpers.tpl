{{- define "zimbra-archivage.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "zimbra-archivage.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "zimbra-archivage.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "zimbra-archivage.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Sélecteur d'un composant : appeler avec (dict "root" $ "component" "backend") */}}
{{- define "zimbra-archivage.selectorLabels" -}}
app.kubernetes.io/name: {{ include "zimbra-archivage.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "zimbra-archivage.backendName" -}}
{{- printf "%s-backend" (include "zimbra-archivage.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "zimbra-archivage.frontendName" -}}
{{- printf "%s-frontend" (include "zimbra-archivage.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
