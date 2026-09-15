# vaultwarden

Bitwarden-compatible vault, served at `vault.88288338.xyz`.

## Before the first sync

The Application and the AppProject destination are not in this repo -- they
live in `ansible-local-network` under `playbooks/kubernetes/cluster/argocd/`
(`manifests/vaultwarden-app.yaml`, a destination entry in
`manifests/project.yaml`, and a task in `argocd.yaml`). Argocd rejects the
application outright until the project lists the namespace, so both halves
have to land before this syncs.

```bash
ansible-playbook site.yaml --tags role-cluster
```

## Create the sealed secret

The deployment reads SMTP credentials (and the admin token, if that is ever
turned on) from a secret named `vaultwarden`. Generate it and commit the
sealed form into `charts/local/templates/`; the pod stays in
`CreateContainerConfigError` until it exists.

```bash
kubectl create secret generic vaultwarden --namespace vaultwarden --dry-run=client -o yaml --from-literal=smtp-username=<brevo-login> --from-literal=smtp-password=<brevo-smtp-key> | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > vaultwarden/charts/local/templates/sealed-secret.yaml
```

Use the Brevo **SMTP key**, not the account password, and not the v3 API key
-- they are three different strings in that account. It is the same credential
`mailserver` already holds as `RELAY_PASSWORD`; `mailserver/README.md` covers
where it comes from, and its `KUBECONFIG` caveat applies to the command above
too -- kubeseal resolves that path at call time and a relative one fails
confusingly.

Holding that key in two places is the argument for the other arrangement:
point `smtp.host` at `mailserver.mailserver.svc.cluster.local` and let that
server relay, which also gets this mail DKIM-signed by 88288338.xyz instead of
by Brevo. It needs a submission account for vaultwarden in
`postfix-accounts.cf`, and `mailserver-tls` is issued for `mail.88288338.xyz`
and will not match the in-cluster service name -- so that route means either
addressing it by its public name through the hairpin, or relaxing certificate
verification on a connection that never leaves the cluster. Brevo direct
avoids the question and pays for it with the second copy of the key.

If `admin.enabled` is ever set, add `--from-literal=admin-token=<argon2-phc>`
to the same command. That value is an Argon2 PHC string, not a password:

```bash
kubectl -n vaultwarden exec deploy/vaultwarden -- /vaultwarden hash --preset bitwarden
```

## Create the first account

There is no bootstrap account. `signups.allowed` is `false`, so:

1. set it `true`, commit, wait for the sync
2. register at `https://vault.88288338.xyz/#/register`
3. set it back to `false`, commit

Between 1 and 3 the registration page is open to anyone who finds the name, so
do not walk away in the middle. Confirm afterwards that the page rejects a new
registration.

Then, on the way in: turn on TOTP two-factor from the web vault, and print the
recovery code. Email 2FA exists but is the weakest of the options here, and it
depends on the same SMTP that the vault would need in order to tell you
anything is wrong.

## Backups

The volume is picked up by longhorn's `daily-backup` recurring job with no
configuration -- every volume gets `recurring-job-group.longhorn.io/default:
enabled` on creation -- so it lands in `s3://lrascao-backups@eu-west-1/` at
02:00 with seven days retained, like everything else in the cluster.

That is the right cadence for node-red and a thin one for a vault: up to 24h
of vault changes lost in a restore, and silent corruption noticed on day eight
is not recoverable. An hourly job narrows the first window. It is not in the
chart because recurring jobs live in `longhorn-system` and storage classes are
cluster-scoped, and the `home-cluster` AppProject permits neither:

```bash
kubectl apply -f - <<'YAML'
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata:
  name: vaultwarden-hourly
  namespace: longhorn-system
spec:
  cron: "0 * * * *"
  task: backup
  retain: 24
  concurrency: 1
YAML
```

```bash
kubectl apply -f - <<'YAML'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: longhorn-vaultwarden
provisioner: driver.longhorn.io
allowVolumeExpansion: true
parameters:
  numberOfReplicas: "3"
  staleReplicaTimeout: "30"
  fsType: ext4
  dataEngine: v1
  backupTargetName: default
  # Both, deliberately: the hourly job gives 24h of fine-grained history and
  # the default group keeps the seven daily copies. A volume carrying its own
  # recurring-job label stops inheriting the default group, so leaving the
  # group out here would silently trade a week of history for a day.
  recurringJobSelector: '[{"name":"vaultwarden-hourly","isGroup":false},{"name":"default","isGroup":true}]'
YAML
```

Then point `storageClassName` in `charts/local/templates/pvc.yaml` at
`longhorn-vaultwarden`. A storage class is immutable on an existing PVC, so
doing this after the fact means restoring into a new claim rather than editing
the old one -- cheaper to decide before the first sync.

Neither of these replaces an occasional encrypted vault export kept outside
the cluster. A backup you have never restored is a hypothesis.

## Upgrades

Vaultwarden implements the Bitwarden API and has to keep pace with the
clients, which update themselves from the app stores. Check the release notes
for the client version each release claims compatibility with before bumping
`image` in `values.yaml`.
