# kafka

The Kafka behind Catalyst's managed pub/sub. Like `postgresql/`, it is an
external dependency of the catalyst chart -- `catalyst/values.yaml` points
`external_kafka.brokers` at `kafka.kafka.svc.cluster.local:9092` -- installed
by hand on 2026-11-04 and captured here afterwards. ArgoCD adopted the running
StatefulSet.

## `kraft.clusterId` is load-bearing

It is not a credential; it is the cluster's identity, and it is also written
into `meta.properties` inside the broker's volume. The chart generates it the
same way it generates passwords, so without pinning it every ArgoCD sync would
propose a new one and the broker would refuse to start with
`InconsistentClusterIdException` on its next restart. Read back with:

```bash
kubectl exec -n kafka kafka-controller-0 -- grep cluster.id /bitnami/kafka/data/meta.properties
```

## SASL passwords

`sasl.existingSecret` points at `kafka-credentials`, a SealedSecret in
`charts/local/templates/`, for the same reason the Postgres password is not in
values.yaml: unset, the chart invents new passwords on every render. It holds
three keys the chart expects -- `client-passwords`, `inter-broker-password`
and `controller-password`. The last two are internal to the broker, so
changing them means a restart, not just a client reconnect.

The client password is sealed a second time into `catalyst-kafka` in
`cra-agent`, which is what the agent reads. The two must stay in step. Note
the agent wants SASL/PLAIN, not the Kafka security protocol -- see the
comments in `catalyst/values.yaml`.

Re-seal from what is live with the same pipe shape as `postgresql/README.md`,
substituting `kafka-credentials` and namespace `kafka`.

## Single broker

`extraConfig` pins every replication factor to 1 because there is one
controller and nothing to replicate to. `message.max.bytes` is raised to 4MB
for the payloads the shopping agent passes through pub/sub.

`image.repository` is `bitnamilegacy/kafka`: Bitnami moved its free images out
of `bitnami/`, and the chart's default no longer resolves.

## Leftover from the hand install

As with `postgresql/`, the `sh.helm.release.v1.kafka.*` secret here predates
ArgoCD and is safe to delete after a successful sync.
