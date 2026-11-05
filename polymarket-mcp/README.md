# polymarket-mcp

[fzlzjerry/polymarket-mcp](https://github.com/fzlzjerry/polymarket-mcp) (PyPI
`mcp-polymarket` 0.1.0) as an MCP server for the Catalyst agents: 14
read-only market-data tools, no trading, no keys, no state.

One pod, two containers. `mcp` runs the server with `uvx` on the pod's
loopback; `auth` is nginx on `:8080`, the only published port, and refuses
anything without `Authorization: Bearer <token>`. The server has no auth of
its own, and flannel does not enforce NetworkPolicy, so without the proxy any
pod in the cluster could call it.

Two things about the upstream package that the proxy depends on:

- The command is `mcp-polymarket`, the same as the package name. There is no
  `polymarket-mcp` executable, whatever older notes say.
- The MCP SDK builds the server with DNS rebinding protection on (Host must be
  `127.0.0.1:*` or `localhost:*`), and `--host 0.0.0.0` does not turn it off.
  Called directly as `polymarket-mcp.polymarket-mcp.svc.cluster.local`, every
  request gets `421 Invalid Host header`. nginx forwards with its default
  `Host: 127.0.0.1:8000`, which passes. Don't add `proxy_set_header Host`.

## Before the first sync

The Application and the AppProject destination live in `ansible-local-network`
(`manifests/polymarket-mcp-app.yaml`, the `polymarket-mcp` destination in
`manifests/project.yaml`, a task in `argocd.yaml`). Push this side first, with
the sealed secret, then from `ansible-local-network`:

```bash
kubectl apply -f playbooks/kubernetes/cluster/argocd/manifests/project.yaml -f playbooks/kubernetes/cluster/argocd/manifests/polymarket-mcp-app.yaml
```

## Create the sealed secret

Hex only: the token is pasted into an nginx string, where a `$` or `"` would
break the config. From the repo root:

```bash
POLYMARKET_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic polymarket-mcp-token --namespace polymarket-mcp --dry-run=client -o yaml --from-literal=MCP_BEARER_TOKEN="$POLYMARKET_TOKEN" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > polymarket-mcp/sealed-secret.yaml
```

Keep the token in vaultwarden; the MCPServer header needs it.

## Register it in Catalyst

```bash
read -rs POLYMARKET_TOKEN
diagrid mcpserver create polymarket --project test --url http://polymarket-mcp.polymarket-mcp.svc.cluster.local:8080/mcp --header Authorization:"Bearer $POLYMARKET_TOKEN" --description "Polymarket public market data, read-only" --wait
diagrid mcpserver tools list polymarket --project test
diagrid mcpserver access grant polymarket --project test --caller agent1 --allow-tools polymarket_search,polymarket_list_markets,polymarket_get_market,polymarket_list_events,polymarket_get_event,polymarket_trending_markets,polymarket_get_live_price,polymarket_get_order_book,polymarket_get_price_history,polymarket_get_user_positions,polymarket_get_user_activity,polymarket_get_market_holders,polymarket_get_user_value,polymarket_market_snapshot
```

The grant names all 14 tools instead of `'*'`, so a later version that adds a
tool -- a trading one, say -- is not granted by default.

## Bumping

uvx installs the package on every start, so PyPI must be reachable whenever
the pod restarts. `--exclude-newer` freezes the whole dependency resolution
at a date (it resolved mcp 1.30.0, httpx 0.28.1, pydantic 2.14.2 on
2026-11-05); move it together with the version, then re-run `tools list` and
diff it against the grant.
