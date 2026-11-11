# postgresql

The shared Postgres that backs Catalyst. It is deliberately outside the
catalyst chart -- `catalyst/values.yaml` reaches it through
`external_postgresql`, and `catalyst/README.md` describes it as an external
dependency -- but it was installed by hand on 2026-11-04 and lived in no repo
until this chart. Adopted by ArgoCD, which took ownership of the running
StatefulSet rather than replacing it.

## The password is not in values.yaml, and must not be

`auth.postgresPassword` is unset on purpose. Left unset, the chart's
`secrets.yaml` falls back to `randAlphaNum 24`, and the `lookup` that would
otherwise reuse the live secret returns nothing under `helm template` -- which
is how ArgoCD renders, since it never talks to the cluster during rendering.
Setting the password here instead would put it in git.

So `auth.existingSecret` points at `postgresql-credentials`, a SealedSecret in
`charts/local/templates/`. It carries the password the database was
initialised with. Changing it here does **not** change the password Postgres
expects -- that one lives in the data directory -- so the two would simply
stop matching and the agent would fail to authenticate.

The same password is sealed a second time into `catalyst-postgres` in
`cra-agent` (see `catalyst/README.md`), which is what the agent actually
reads. Rotating means doing both, plus an `ALTER USER` against the running
database, in that order.

To re-seal from what is live, without the plaintext touching disk:

```bash
kubectl get secret postgresql-credentials -n postgresql -o json | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > postgresql/charts/local/templates/sealed-secret.yaml
```

## Immutable bits

`primary.persistence.size` is 1Gi because that is what the live
`volumeClaimTemplate` says. `volumeClaimTemplates` are immutable: changing
this value makes ArgoCD fail the sync rather than resize anything. Growing the
volume means expanding the Longhorn PVC and recreating the StatefulSet with
`--cascade=orphan`.

`primary.extendedConfiguration` sets `wal_level = logical`, which Catalyst's
Dapr state store needs.

## Leftover from the hand install

The `sh.helm.release.v1.postgresql.*` secrets in this namespace are from the
original `helm install`. ArgoCD does not use them. They are safe to delete
once a sync has gone through, and worth deleting: while they exist,
`helm uninstall postgresql` would tear out resources ArgoCD now manages.
