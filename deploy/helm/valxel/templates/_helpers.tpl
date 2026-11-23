{{/* Names: everything is <release>-<part>, so two installs can share a namespace. */}}
{{- define "valxel.name" -}}{{ .Release.Name | trunc 40 | trimSuffix "-" }}{{- end -}}
{{- define "valxel.api" -}}{{ include "valxel.name" . }}-api{{- end -}}
{{- define "valxel.worker" -}}{{ include "valxel.name" . }}-worker{{- end -}}
{{- define "valxel.provisioner" -}}{{ include "valxel.name" . }}-provisioner{{- end -}}
{{- define "valxel.gateway" -}}{{ include "valxel.name" . }}-llm-gateway{{- end -}}
{{- define "valxel.sink" -}}{{ include "valxel.name" . }}-audit-sink{{- end -}}
{{- define "valxel.kafka" -}}{{ include "valxel.name" . }}-audit{{- end -}}
{{- define "valxel.sessionSA" -}}agent-session{{- end -}}
{{- define "valxel.egressProxy" -}}{{ include "valxel.name" . }}-egress-proxy{{- end -}}
{{- define "valxel.egressProxyHost" -}}{{ include "valxel.egressProxy" . }}.{{ .Release.Namespace }}.svc.cluster.local{{- end -}}
{{- define "valxel.egressProxyUrl" -}}http://{{ include "valxel.egressProxyHost" . }}:3128{{- end -}}
{{- define "valxel.gatewayHost" -}}{{ include "valxel.gateway" . }}.{{ .Release.Namespace }}.svc.cluster.local{{- end -}}

{{/* Where sessions may connect, through the egress proxy: the egress list (a copy of
config/egress.yaml) and the hosts of the customer's registry mirrors. A YAML list. */}}
{{- define "valxel.egressHosts" -}}
{{- $hosts := list -}}
{{- range .Values.sessions.egress }}{{ $hosts = append $hosts . }}{{ end -}}
{{- range $k, $v := .Values.sessions.registryMirrors }}{{- if $v }}{{ $hosts = append $hosts (urlParse $v).hostname }}{{- end }}{{ end -}}
{{- $hosts | uniq | toYaml -}}
{{- end -}}

{{- define "valxel.labels" -}}
app.kubernetes.io/name: valxel
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* An image by digest: required, and never a tag. */}}
{{- define "valxel.digest" -}}
{{- $d := required (printf "%s: set its digest (sha256:...); images are deployed by digest, never by tag" .what) .image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $d) -}}
{{- fail (printf "%s: digest must be sha256: and 64 hex characters, not %q" .what $d) -}}
{{- end -}}
{{ .image.repository }}@{{ $d }}
{{- end -}}
{{- define "valxel.image" -}}{{ include "valxel.digest" (dict "what" "image" "image" .Values.image) }}{{- end -}}

{{- define "valxel.gatewayUrl" -}}http://{{ include "valxel.gateway" . }}.{{ .Release.Namespace }}.svc.cluster.local:8080{{- end -}}

{{/* Kafka: bootstrap, CA Secret, and each user's password Secret, by mode. */}}
{{- define "valxel.kafkaBootstrap" -}}
{{- if eq .Values.kafka.mode "strimzi" -}}{{ include "valxel.kafka" . }}-kafka-bootstrap.{{ .Release.Namespace }}.svc:9094
{{- else -}}{{ required "kafka.bootstrap: set the customer's Kafka bootstrap (kafka.mode is external)" .Values.kafka.bootstrap }}{{- end -}}
{{- end -}}
{{- define "valxel.kafkaCa" -}}
{{- if eq .Values.kafka.mode "strimzi" -}}{{ include "valxel.kafka" . }}-cluster-ca-cert
{{- else -}}{{ required "kafka.caSecret: a Secret with the Kafka CA's ca.crt" .Values.kafka.caSecret }}{{- end -}}
{{- end -}}
{{- define "valxel.kafkaUser" -}}
{{- $root := .root -}}
{{- if eq $root.Values.kafka.mode "strimzi" -}}{{ include "valxel.name" $root }}-{{ .user }}
{{- else -}}{{ required (printf "kafka.users.%s: the Secret holding that Kafka user's password" .user) (index $root.Values.kafka.users .user) }}{{- end -}}
{{- end -}}
{{- define "valxel.kafkaUsername" -}}
{{- if eq .root.Values.kafka.mode "strimzi" -}}{{ include "valxel.name" .root }}-{{ .user }}{{- else -}}valxel-{{ .user }}{{- end -}}
{{- end -}}

{{/* The pod-level hardening every Valxel process has. */}}
{{- define "valxel.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "valxel.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* Checks that fail the install with a clear message, before anything is applied. */}}
{{- define "valxel.validate" -}}
{{- if not (has (include "valxel.cloud" .) (list "aws" "gcp" "azure")) -}}
{{- fail (printf "cloud.provider is aws, gcp or azure, not %q" (include "valxel.cloud" .)) -}}
{{- end -}}
{{- if not (hasPrefix "https://" (required "publicUrl: the tool's address, https://..." .Values.publicUrl)) -}}
{{- fail "publicUrl must be https://: the tool is served over TLS only" -}}
{{- end -}}
{{- if not (has .Values.auth.mode (list "github" "cognito" "oidc")) -}}{{- fail "auth.mode is github, cognito or oidc" -}}{{- end -}}
{{- if not (has .Values.auth.oidc.emailVerified (list "claim" "issuer")) -}}{{- fail "auth.oidc.emailVerified is claim or issuer" -}}{{- end -}}
{{- if not (has .Values.kafka.mode (list "strimzi" "external")) -}}{{- fail "kafka.mode is strimzi or external" -}}{{- end -}}
{{- if not (has .Values.networkPolicy.flavor (list "cilium" "kubernetes")) -}}{{- fail "networkPolicy.flavor is cilium or kubernetes" -}}{{- end -}}
{{- if not (regexMatch "^[a-f0-9]{64}$" (required "gateway.toolKeySha256: the SHA-256 of the tool's gateway key, in hex" .Values.gateway.toolKeySha256 | toString)) -}}
{{- fail "gateway.toolKeySha256 must be 64 hex characters" -}}
{{- end -}}
{{- range $k, $v := .Values.sessions.registryMirrors -}}
{{- if and $v (not (hasPrefix "https://" $v)) -}}{{- fail (printf "sessions.registryMirrors.%s must be https://" $k) -}}{{- end -}}
{{- end -}}
{{- if eq .Values.sessions.namespace .Release.Namespace -}}
{{- fail "sessions.namespace must differ from the release's namespace: sessions are isolated in one of their own" -}}
{{- end -}}
{{- end -}}

{{/* The installation's cloud and region (ADR 0010); awsRegion is the old name for cloud.region. */}}
{{- define "valxel.cloud" -}}{{ (.Values.cloud | default dict).provider | default "aws" }}{{- end -}}
{{- define "valxel.cloudRegion" -}}{{ (.Values.cloud | default dict).region | default .Values.awsRegion | default "" }}{{- end -}}
