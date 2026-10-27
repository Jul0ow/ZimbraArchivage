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

{{/* Nom du Secret contenant la clé des sessions */}}
{{- define "zimbra-archivage.sessionSecretName" -}}
{{- .Values.session.existingSecret | default (printf "%s-session" (include "zimbra-archivage.fullname" .)) }}
{{- end }}

{{/* Nom du Secret contenant l'URL du proxy vers Zimbra (vide sans proxy) */}}
{{- define "zimbra-archivage.proxySecretName" -}}
{{- if .Values.zimbra.proxy.existingSecret }}
{{- .Values.zimbra.proxy.existingSecret }}
{{- else if .Values.zimbra.proxy.url }}
{{- printf "%s-proxy" (include "zimbra-archivage.fullname" .) }}
{{- end }}
{{- end }}

{{/* Placement d'un pod : appeler avec la section du composant (.Values.backend ou .Values.frontend) */}}
{{- define "zimbra-archivage.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Sondes d'un conteneur : appeler avec (dict "path" "/api/health" "values" .Values.backend) */}}
{{- define "zimbra-archivage.probes" -}}
readinessProbe:
  httpGet:
    path: {{ .path }}
    port: http
  {{- toYaml .values.readinessProbe | nindent 2 }}
livenessProbe:
  httpGet:
    path: {{ .path }}
    port: http
  {{- toYaml .values.livenessProbe | nindent 2 }}
{{- end }}

{{/* Service d'un composant : appeler avec (dict "root" $ "component" "backend" "name" … "values" .Values.backend) */}}
{{- define "zimbra-archivage.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ .name }}
  labels:
    {{- include "zimbra-archivage.labels" .root | nindent 4 }}
    app.kubernetes.io/component: {{ .component }}
spec:
  type: {{ .values.service.type }}
  selector:
    {{- include "zimbra-archivage.selectorLabels" (dict "root" .root "component" .component) | nindent 4 }}
  ports:
    - name: http
      port: {{ .values.port }}
      targetPort: http
{{- end }}
