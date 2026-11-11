# catalyst

The Diagrid Catalyst agent for the `homelab` private region, in `cra-agent`,
plus what this cluster puts in front of it: passthrough HTTPProxies for the
region's names and the sealed agent credentials.

Clients use the region's managed names, `*.<ingress-wildcard-domain>` (see
ConfigMap `region-details`). The house DNS answers them locally with the
ingress VIP (`db.catalyst` in ansible-local-network) -- Diagrid's public
answer is a CNAME to `diagrid.home`, which no public resolver can follow --
and contour hands the TLS stream to the gateway's `8443` listener without
opening it. Recreating the region changes the domain, and both places with
it.

The Application's release name is pinned to `catalyst`. That is what keeps
every object's name and selector identical to the hand-installed helm release
this replaced; a different release name renames the Deployments, and their
selectors are immutable.

## Never `helm uninstall catalyst`

The chart carries a `post-delete` hook that uninstalls the agent's dapr
releases. ArgoCD runs it as a `PostDelete` hook, so it fires when the
Application is deleted -- and `helm uninstall` would fire it too, on top of
deleting everything ArgoCD manages. To take catalyst down, delete the
Application and mean it.

The agent installs helm releases of its own (`dapr` in `root-dapr-system`,
`catalyst-otel-*` in `cra-agent`). Those are not in this chart and ArgoCD
neither tracks nor prunes them.

## Credentials

Two SealedSecrets in `charts/local/templates/`, split so the token can be
re-sealed without touching the database credentials:

- `catalyst-join-token` (key `join_token`), read by the agent through
  `agent.existingJoinTokenSecret`. The token expires 2026-11-11 13:48 UTC,
  and that is fine: the agent presents it on every boot, the control plane
  accepts a joined host's token expired, and the admin rejoin in "Dapr PKI"
  takes this same token. Leave it. Replace it only if Diagrid regenerates the
  host's token, since every later join must present the current one:

  ```bash
  kubectl create secret generic catalyst-join-token --namespace cra-agent --dry-run=client -o yaml --from-literal=join_token=<token> | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > catalyst/charts/local/templates/sealed-secret-join-token.yaml
  ```

- `catalyst-postgres` (keys `host`, `port`, `username`, `password`), the
  shared Postgres in namespace `postgresql`, read by both the agent and
  management through `external_postgresql.existing_secret_name`. Same command
  shape, with those four `--from-literal`s.

Run them from the repo root. If `KUBECONFIG` is set it must be an absolute
path -- see `mailserver/README.md`.

## Dapr PKI

The region's root Dapr Sentry (`root-dapr-system`) issues every sidecar's,
the agent's and the Dapr control plane's mTLS identity. It was installed
without a PKI, so Sentry generated its own CA: an Ed25519 root, fingerprint
`F6:0F:C9:BE:...:69:D0`, valid 2026-11-04 to 2027-11-04, which nothing renews.
It now issues from ours, through `catalyst.agent.config.internal_dapr.pki`:

| Piece | Where | Changes |
|---|---|---|
| Root, ECDSA P-256, 10 years | Minted offline, sealed into `charts/local/templates/sealed-secret-dapr-root-ca.yaml` (Secret `diagrid/catalyst-dapr-root-ca`) | Only with a rejoin |
| Sentry's issuer, 1 year | cert-manager `Certificate` `diagrid/catalyst-dapr-issuer` in `charts/local/templates/dapr-pki.yaml` | Renewed by cert-manager, picked up by the agent within a minute |
| Trust anchors | `charts/local/files/dapr-trust-anchors.pem`, rendered as ConfigMap `diagrid/catalyst-dapr-trust-anchors` | Only with a rejoin |

All of it lives in namespace `diagrid` (`pkiNamespace` in
`charts/local/values.yaml`, which the chart creates), not in `cra-agent`, so
the two private keys are kept apart from the agent's namespace. The agent
reads the issuer Secret and the anchors there with its cluster-wide read on
Secrets and ConfigMaps.

The root is sealed rather than made by cert-manager because the control
plane pins the trust anchors on the region's first join, and refuses any later
join whose anchors hold a different set of certificates (a 409, `already
consumed ... with a different CA trust anchor`). A cert-manager root would be
re-minted if its Secret were lost, and an issuer under a root the control plane
has not pinned breaks the region. Keep a copy of `root.key` outside the
cluster too (Vaultwarden): the SealedSecret is only as recoverable as the
sealed-secrets controller's key.

The anchors file holds the Sentry-generated root first, then ours. Keep the
old one: Dapr verifies signed workflow history against the current anchors,
and history signed under a root that has left them is tombstoned as tampered.
Once expired it only verifies what it signed while it was valid. Dropping it
is an anchor change like any other.

### Changing the trust anchors

Any change to `dapr-trust-anchors.pem`, including the first switch to this
PKI and a new root, goes in this order. The agent reads the anchors only at
boot, and on boot installs them into Sentry *before* it joins, so the control
plane has to know them first.

1. **Mint and seal the root** (only for a new root). Outside the repo:

   ```bash
   P=~/catalyst-dapr-pki; mkdir -m 700 -p $P
   printf '[req]\ndistinguished_name = dn\n[dn]\n[root]\nbasicConstraints = critical, CA:TRUE\nkeyUsage = critical, digitalSignature, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n' > $P/ext.cnf
   openssl ecparam -name prime256v1 -genkey -noout -out $P/root.key
   openssl req -x509 -new -key $P/root.key -sha256 -days 3650 -subj "/O=homelab/CN=homelab-catalyst-dapr-root" -config $P/ext.cnf -extensions root -out $P/root.crt
   ```

   From the repo root, seal it and add it to the anchors after the roots
   already there:

   ```bash
   kubectl create secret tls catalyst-dapr-root-ca --namespace diagrid --dry-run=client -o yaml --cert ~/catalyst-dapr-pki/root.crt --key ~/catalyst-dapr-pki/root.key | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > catalyst/charts/local/templates/sealed-secret-dapr-root-ca.yaml
   cat ~/catalyst-dapr-pki/root.crt >> catalyst/charts/local/files/dapr-trust-anchors.pem
   ```

   Store `root.key` away from this machine, then `rm -P ~/catalyst-dapr-pki/root.key`.

2. **Push the PKI objects alone**, without touching `values.yaml`. The agent
   does not read them yet. Once ArgoCD has synced, the issuer must chain to the
   anchors:

   ```bash
   kubectl -n diagrid get certificate catalyst-dapr-issuer
   kubectl -n diagrid get secret catalyst-dapr-issuer -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl verify -CAfile catalyst/charts/local/files/dapr-trust-anchors.pem
   ```

   `READY True`, then `stdin: OK`.

3. **Rejoin**, which replaces the pinned anchors with the file's. It is
   Diagrid's admin API (`diagrid-adm`, built from cloudgrid's `tools/admin`),
   and takes the token the host joined with -- the one already sealed in
   `catalyst-join-token`, read straight from the cluster below -- which it
   accepts expired:

   ```bash
   export DIAGRID_ADMIN_ENABLE_WRITE_ACCESS=true
   diagrid-adm --env production-r1 orgs rejoin-region 7a9817cc-30bf-4bc5-89b3-99687a01d85c --region homelab --join-token "$(kubectl -n cra-agent get secret catalyst-join-token -o jsonpath='{.data.join_token}' | base64 -d)" --ca-bundle catalyst/charts/local/files/dapr-trust-anchors.pem --reason "homelab: root Sentry onto own PKI"
   ```

   `Private region CA rotation accepted.` The running agent is unaffected,
   but from here until step 4 lands an agent restart fails with the 409, so do
   step 4 straight away.

4. **Push the change that makes the agent read them**: the first time, the
   `internal_dapr.pki` block in `values.yaml`; after that, the anchors file
   itself. The agent restarts, installs the new issuer and anchors into Sentry
   and joins. Not within a few minutes of 10:00 UTC, when the
   `jwt-key-rotation` CronJob rewrites Sentry's Secret.

   ```bash
   kubectl -n cra-agent rollout status deploy/agent --timeout=10m
   kubectl -n cra-agent logs deploy/agent | grep -E 'loaded dapr trust anchors|error joining region'
   ```

   The agent copies the anchors into every `prj-*` namespace and restarts
   their sidecars, but not the Dapr control plane or its own siblings:

   ```bash
   kubectl -n root-dapr-system rollout restart deploy/dapr-operator sts/dapr-placement-server sts/dapr-scheduler-server
   kubectl -n cra-agent rollout restart deploy -l app.kubernetes.io/instance=catalyst
   kubectl get pods -A | awk '$1 ~ /^prj-/ && $4 ~ /CrashLoop|Init:/'
   ```

   If the agent exits on the 409 instead, the pinned set is not the file's:
   compare the `provided sha256` in that error with the rejoin, and rejoin
   again with the file. Fix forward rather than reverting `values.yaml`:
   Sentry already holds the new CA by then, and how an agent without the
   `pki` block treats that state is untested.

cloudgrid has the reasoning behind each step:
`docs/content/runbooks/catalyst-shared-pki-root.md` and
`catalyst-region-sentry-ca-rotation.md`.

## Which release to install next

`catalyst.agent.config.release_availability` is on, so the agent asks Diagrid's
resolver every five minutes and publishes the answer into ConfigMap
`catalyst-release-availability` in `cra-agent`:

```bash
kubectl -n cra-agent get cm catalyst-release-availability -o yaml
```

Read `state` first (`current`, `expired` or `unavailable`). `nextVersion` is the
release to install now, `currentVersion` is what the region is observed
running, and `expiresAt` is about fifteen minutes out: a decision is a fact
with a shelf life, not a standing instruction. `enabled` alone publishes
nothing -- the resolver `endpoint` is required, and without it the agent only
logs a warning.

**The version in `Chart.yaml` is no longer the one deployed.** ArgoCD renders
this chart through a config management plugin, `catalyst-release-v1.0`, which
rewrites that dependency version before templating. It lives in
ansible-local-network, under `playbooks/kubernetes/cluster/argocd/`:

- `manifests/catalyst-release-cmp.yaml` -- the plugin and the script that does
  the work
- `templates/values/argocd.yaml.j2` -- the `catalyst-release` sidecar on
  argocd-repo-server, which is why this chart grants `argocd-repo-server` `get`
  on that one ConfigMap
- `manifests/catalyst-app.yaml` -- the Application, which now names the plugin
  where it used to carry `helm.releaseName`. The release name moved into the
  script and is still load-bearing there

What it renders, in order: `nextVersion`, when `state` is `current`, the
version is non-empty, `expiresAt` has not passed, and helm resolves the tag to
the published `nextChartDigest`; otherwise `currentVersion`, so a stale or
missing answer holds the region where it is rather than dragging it back to
whatever git pins; otherwise the version in `Chart.yaml`, which is what
bootstraps a region that has published nothing. Whether the version carries an
`-rc.` picks `candidate/` or the GA path, so a promotion to GA moves the
repository as well as the version.

At the channel head there is nothing to install, and the answer says so by
leaving `nextVersion` empty with `reason: ALREADY_AT_DESTINATION` -- which the
plugin treats as "hold what is running", the same as a stale answer. This
region is on `channel: candidate`, `mode: follow`. The one branch that has
never run on real values is the digest check: `nextChartDigest` is empty until
there is a hop to make, and it is read as the chart's OCI manifest digest,
which is what helm prints and is NOT `nextReleaseDigest` (a different digest,
over Diagrid's release catalog). If that reading is wrong the first hop holds
`currentVersion` and logs `digest mismatch`, so it fails safe and says so.

Every decision is a line in the sidecar's log:

```bash
kubectl -n argocd logs deploy/argocd-repo-server -c catalyst-release | grep catalyst-release:
```

### When it takes effect

Not the moment Diagrid publishes. The Application reconciles every 120s, but
each reconcile is served from ArgoCD's manifest cache, keyed by git revision
and kept for `--repo-cache-expiration` -- 24h by default and not overridden
here. So a release Diagrid publishes lands within a day, or at once on either
of the two things that miss that cache: a commit to this repo, which is a new
revision and so a new key, or a hard refresh:

```bash
kubectl -n argocd annotate app catalyst argocd.argoproj.io/refresh=hard --overwrite
```

Restarting repo-server does **not** shorten it -- the cache lives in Redis,
not in the pod. Lowering the TTL is one line in the argo-cd values
(`configs.params."reposerver.repo.cache.expiration"`) and it applies to every
app in this repo, not just this one.

To stop the region moving at all, hold the channel at Diagrid's end
(`diagrid region release hold --reason ... --review-at ...`). The resolver then
offers no `nextVersion` and the plugin holds `currentVersion`.

## Bumping the chart

Change the `catalyst` version in `Chart.yaml` and run `helm dependency update`
in this directory so `Chart.lock` follows. Release candidates come from
`oci://public.ecr.aws/diagrid/candidate`, GA versions from
`oci://public.ecr.aws/diagrid`.

Since the plugin above, this sets the fallback rather than the deployed
version: the region follows the channel, and a bump here only shows up on a
region that has published no answer yet. To move the region deliberately, move
it at Diagrid's end -- the channel, or a hold.
