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
  `agent.existingJoinTokenSecret`. The token expires 2026-11-11 13:48 UTC.
  Whether the agent needs it again once it has joined is not established; if
  it does, get a new one from Diagrid and re-seal:

  ```bash
  kubectl create secret generic catalyst-join-token --namespace cra-agent --dry-run=client -o yaml --from-literal=join_token=<token> | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > catalyst/charts/local/templates/sealed-secret-join-token.yaml
  ```

- `catalyst-postgres` (keys `host`, `port`, `username`, `password`), the
  shared Postgres in namespace `postgresql`, read by both the agent and
  management through `external_postgresql.existing_secret_name`. Same command
  shape, with those four `--from-literal`s.

Run them from the repo root. If `KUBECONFIG` is set it must be an absolute
path -- see `mailserver/README.md`.

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
