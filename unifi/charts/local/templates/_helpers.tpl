{{/*
On-prem only, required, for every workload in this chart.

Spelled DoesNotExist and NOT NotIn [cloud]. Cloud nodes join carrying
node.homelab/location=cloud (cloud/cloud-init.sh), but node1-4 carry no
location label at all -- and a NotIn match expression does not match a node
that is missing the key. The obvious spelling therefore excludes every LAN
node and leaves the whole chart Pending on what reads like a capacity
problem. This is the same trap documented in the ansible repo's
docs/inference-lab-and-healthcheck.md.

Belt to the braces of having no toleration for location=cloud:NoSchedule,
which is what actually keeps these pods off a burst node today. The affinity
is what keeps them off one if that taint is ever dropped.

Storage is covered by the same rule from the other end: Longhorn does not run
on cloud nodes, so a PVC in this chart cannot bind across the tunnel even if
a pod somehow landed there.
*/}}
{{- define "local.onPremAffinity" -}}
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          - key: node.homelab/location
            operator: DoesNotExist
{{- end -}}

{{/*
Mongo credentials, as env vars, from the SealedSecret-derived Secret.
Both containers need the same four values for different reasons: mongo to
create the users on first run, the controller to log in with them.
*/}}
{{- define "local.mongoEnv" -}}
- name: MONGO_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretName }}
      key: MONGO_USER
- name: MONGO_PASS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretName }}
      key: MONGO_PASS
{{- end -}}
