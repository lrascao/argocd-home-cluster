# continente-mcp

[davidmarqu3s/continente-mcp](https://github.com/davidmarqu3s/continente-mcp)
(npm `continente-mcp`), serving MCP (streamable HTTP) at `/mcp` for the
Catalyst agents. Nine tools over Continente.pt: product search, the account's
favourites, the cart, and order history.

There is no Continente API. The server drives a real headless Chromium through
Playwright, signs in as Luis with his own account password, and scrapes the
pages. That shapes everything below.

Catalyst never runs this. The MCPServer registered in project `test` is only a
URL; the sidecar Catalyst runs for it calls
`http://continente-mcp.continente-mcp.svc.cluster.local:8000/mcp` and enforces
the per-caller tool grants. The registration and grants live in Catalyst, not
here.

## Why the packages are installed at start

This repo builds no images, so the pod starts from Microsoft's Playwright
image -- which carries Chromium and its OS libraries, the hard part -- and
`npm install`s three exact versions into the tmp volume before `exec`ing the
gateway. Same trade-off as polymarket-mcp's `uvx`: the npm registry has to be
reachable on every restart.

`playwright`'s pin is the one that bites. `continente-mcp` depends on
`^1.59.1`, so an unpinned install takes the newest release and then cannot
find its matching Chromium build in the image (`Please update docker image as
well`). The pin in `statefulset.yaml` is the image's own Playwright version:
**bump the image tag, its digest and `playwright@` together, or not at all.**

`continente-mcp` speaks stdio only, so [supergateway](https://github.com/supercorp-ai/supergateway)
wraps it in streamable HTTP. It also does the bearer check
(`SUPERGATEWAY_API_KEY`) and serves `/healthz` ahead of that check, so there
is no nginx sidecar here -- unlike polymarket-mcp, which needed one.

It runs `--stateful`. In the stateless mode each HTTP POST gets a child
process of its own, which here means a fresh Node and a cold Chromium per tool
call. Stateful gives each MCP session one child, reaped after 15 minutes idle
(`--sessionTimeout`).

## Before the first sync

The Application and the AppProject destination live in
`ansible-local-network` (`manifests/continente-mcp-app.yaml`, the
`continente-mcp` destination in `manifests/project.yaml`, a task in
`argocd.yaml`). Push this side first, with the sealed secret, then apply that
side from `ansible-local-network`:

```bash
kubectl apply -f playbooks/kubernetes/cluster/argocd/manifests/project.yaml -f playbooks/kubernetes/cluster/argocd/manifests/continente-mcp-app.yaml
```

## Create the sealed secret

One secret, three keys: the Continente account's own email and password, and
the bearer token Catalyst presents. The pod stays in
`CreateContainerConfigError` until it exists. From the repo root:

Read the two credentials in without putting them in the shell history
(`read -r`/`-rs`, not `-p`, so this works in zsh as well as bash):

```bash
echo -n 'Continente email: '; read -r CONT_EMAIL; echo -n 'Continente password: '; read -rs CONT_PW; echo
```

```bash
MCP_KEY=$(openssl rand -hex 32) && kubectl create secret generic continente-credentials --namespace continente-mcp --dry-run=client -o yaml --from-literal=CONTINENTE_EMAIL="$CONT_EMAIL" --from-literal=CONTINENTE_PASSWORD="$CONT_PW" --from-literal=MCP_API_KEY="$MCP_KEY" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > continente-mcp/sealed-secret.yaml && unset CONT_PW && echo "MCP_API_KEY=$MCP_KEY"
```

Put the account password and the printed `MCP_API_KEY` in vaultwarden before
closing the shell -- the token is needed again to register the server in
Catalyst, and nothing else prints it. If `KUBECONFIG` is set it must be an
absolute path -- see `mailserver/README.md`.

## The first login

On a fresh volume there is no `cookies.json`, so the first tool call drives the
login form: email, password, and Continente's own verification checkbox, which
the server clicks and then waits up to 60s for. That first call can take a
minute and may simply fail. The cookies are cached on the volume afterwards, so
this is a once-per-volume cost, and any later call retries the login by itself.

Watch it happen, since nothing else reports it:

```bash
kubectl -n continente-mcp logs -f statefulset/continente-mcp
```

To force a fresh login, delete the cache and let the next call rebuild it:

```bash
kubectl -n continente-mcp exec statefulset/continente-mcp -- rm -f /state/cookies.json
```

## Register it in Catalyst

```bash
read -rs MCP_KEY   # the MCP_API_KEY from vaultwarden
diagrid mcpserver create continente --project test --url http://continente-mcp.continente-mcp.svc.cluster.local:8000/mcp --header Authorization:"Bearer $MCP_KEY" --description "Continente.pt groceries (search, cart, order history) for Luis's account" --wait
diagrid mcpserver tools list continente --project test
diagrid mcpserver access grant continente --project test --caller agent1 --allow-tools search_products,get_favorites,get_cart
```

### What to grant

Grants match tool names. Nothing here can place an order or pay -- there is no
checkout tool, and `get_cart` ends its reply by telling the reader to go to the
website to check out. The worst a granted write does is change what is sitting
in the cart when Luis next opens it.

| Tool | Grant? | Why |
| --- | --- | --- |
| `search_products` | yes | Reads the public catalogue; ranks hits against favourites |
| `get_cart` | yes | Reads the cart |
| `get_favorites` | yes | Cached favourites list |
| `refresh_favorites` | yes | Re-scrapes favourites; a page load, nothing more |
| `get_most_bought` | for an agent that needs it | Derived from order history, so it reads it |
| `get_order_history` | for an agent that needs it | Past orders: what, when, how much |
| `add_to_cart` | only for an agent that should shop | Writes to the real cart |
| `update_cart_item` | only for an agent that should shop | Same, and quantity `0` removes an item |
| `close_session` | no | Housekeeping; drops the browser, next call reopens it |

## Risks this does not solve

- **The secret holds a full account password.** Not scoped, not a token: the
  same credentials as the website, where the account has the order history and
  the saved delivery addresses. The cookies on the volume are account access
  too, and that volume is in Longhorn's `default` group, so the nightly 02:00
  UTC S3 backup carries them. Rotating the password invalidates both.
- **It automates Continente's login verification widget.** The server clicks
  the checkbox and waits for it to turn `Verificado`. That is bot handling on a
  site whose terms do not invite it; the account can be blocked, and nothing
  here would distinguish that from a broken selector.
- **It is a scraper.** Every tool depends on Continente's current CSS classes
  (`.ct-inner-tile-wrap`, `.pwc-tile--price-primary`). A redesign turns tools
  into empty results or errors, not alarms. Prices and minimum quantities it
  reports are parsed out of a page and can be wrong or stale.
- **Scraped text reaches the agent.** Product names and order lines are
  returned as plain text. With a cart-write tool granted, anything the site
  shows is in a position to influence what the agent does next. Keep read-only
  agents read-only.
- **Bumping is two-sided.** The image tag, its digest and the `playwright@`
  pin move together. npm `4.1.1` is also two commits behind the repo's `main`,
  which carries a login retry fix that is not in any release yet.
