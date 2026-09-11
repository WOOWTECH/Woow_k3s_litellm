{{/*
Helper templates for the litellm chart.
Resource names, labels and selectors are fixed (not derived from the release
name): the Cloudflare tunnel routes to these exact Service names, and changing
a selector or pod-template label would restart every pod.
*/}}

{{- define "litellm.ns" -}}
{{ .Values.namespace.name }}
{{- end -}}

{{- define "litellm.mcpNs" -}}
{{ .Values.mcp.namespace }}
{{- end -}}

{{/* Shared label on every object. */}}
{{- define "litellm.partOf" -}}
app.kubernetes.io/part-of: woow-litellm
{{- end -}}

{{/* `annotations:` block with the keep policy, or nothing. */}}
{{- define "litellm.keepAnnotations" -}}
{{- if .Values.keepOnUninstall -}}
annotations:
  helm.sh/resource-policy: keep
{{- end -}}
{{- end -}}

{{/* storageClassName for a component: its own override or the global default. */}}
{{- define "litellm.storageClass" -}}
{{- $ctx := index . 0 -}}
{{- $override := index . 1 -}}
{{ default $ctx.Values.storageClassName $override }}
{{- end -}}

{{- define "litellm.baseUrl" -}}
http://litellm.{{ .Values.namespace.name }}.svc.cluster.local:4000
{{- end -}}

{{- define "litellm.postgresImage" -}}
{{ .Values.postgres.image.repository }}:{{ .Values.postgres.image.tag }}
{{- end -}}

{{/* model_name values from config/config.yaml, comma-separated. */}}
{{- define "litellm.modelNames" -}}
{{- $cfg := .Files.Get "config/config.yaml" | fromYaml -}}
{{- $names := list -}}
{{- range $cfg.model_list -}}
{{- $names = append $names .model_name -}}
{{- end -}}
{{ join "," $names }}
{{- end -}}
