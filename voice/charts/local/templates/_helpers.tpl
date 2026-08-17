{{/*
Placement shared by all three Wyoming services.

Two separate concerns, deliberately kept apart:

  1. LAN-only. Emitted always. See values.yaml for why this is DoesNotExist
     and not NotIn -- node1-4 carry no location label at all, so a NotIn
     match would exclude every one of them and leave the pod Pending with a
     message that reads like the cluster is full.

  2. Spread. Soft anti-affinity against the other voice pods, so whisper,
     piper and openwakeword do not all pile onto the same box. They are each
     modest on their own and the sum of them beside etcd is not. Soft because
     three components on three of four nodes is a preference, not a
     requirement -- a single-node cluster should still get voice.

Callers pass the root context plus an optional avoidNode.
*/}}
{{- define "voice.affinity" -}}
{{- $ctx := .ctx -}}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: {{ $ctx.Values.placement.lanOnlyLabelKey }}
              operator: DoesNotExist
  {{- if .avoidNode }}
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: kubernetes.io/hostname
              operator: NotIn
              values:
                - {{ .avoidNode }}
  {{- end }}
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 50
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              app.kubernetes.io/part-of: voice
{{- end -}}
