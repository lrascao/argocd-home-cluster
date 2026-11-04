# catalyst

The Diagrid Catalyst agent for the `homelab` private region, in `cra-agent`,
plus what this cluster puts in front of it: a passthrough HTTPProxy for
`*.diagrid.home` and the sealed agent credentials.

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

## Bumping the chart

Change the `catalyst` version in `Chart.yaml` and run `helm dependency update`
in this directory so `Chart.lock` follows. Release candidates come from
`oci://public.ecr.aws/diagrid/candidate`, GA versions from
`oci://public.ecr.aws/diagrid`.
