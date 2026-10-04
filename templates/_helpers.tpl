{{/*
Helper templates for the litellm chart.
Resource names, labels and selectors are fixed (not derived from the release
name): the Cloudflare tunnel routes to these exact Service names, and changing
a selector or pod-template label would restart every pod.
*/}}

{{- /*
Object placement. `namespace.name` is EMPTY by default so placement follows
`-n`/`--namespace`. Setting it makes it win over `-n`, so a
`-n <test-ns> -f <real instance values>` rehearsal would write to the REAL
namespace. That happened once on the sibling n8n chart and touched
production. Never set it in any committed instance values file.
*/ -}}
{{- define "litellm.ns" -}}
{{ .Values.namespace.name | default .Release.Namespace }}
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

{{/*
Image reference for a component, double-pinned as repo:tag@sha256 when a digest
is set (the tag stays human-readable; the digest is what the kubelet actually
pulls, so a mutated or re-pushed tag can never change the running image).
Call with the image map, e.g. (include "litellm.imageRef" .Values.litellm.image).
*/}}
{{- define "litellm.imageRef" -}}
{{- if .digest -}}
{{ .repository }}:{{ .tag }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end -}}

{{- define "litellm.litellmImage" -}}
{{ include "litellm.imageRef" .Values.litellm.image }}
{{- end -}}

{{- define "litellm.postgresImage" -}}
{{ include "litellm.imageRef" .Values.postgres.image }}
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
