# options-agent

Several options strategies trading side by side on one Interactive Brokers
**paper** account, each run by a Catalyst managed agent of its own, every half
hour of the US session. Each strategy has a budget in euros, and all of them
trade the same underlyings.

```
cron binding        every 30 min, US session                  ../catalyst/options-<id>-schedule.yaml
  -> POST /v1/runs  a run of options-<id>, in a new thread     ../catalyst/options-<id>.yaml
     -> MCPServer   broker-<id>, sending <id>'s token
        -> broker   the adapter: who is calling, the ledger,   broker-ibkr/
                    the account's and <id>'s hard limits       limits.env, strategies/<id>.toml
           -> IB Gateway, same pod -> IBKR paper account      deployment.yaml

whatsapp-agent -> options-trader, the desk: read-only, sees every strategy
                    -> MCPServer broker, sending the admin token
```

Two strategies to start with, to prove the mechanism more than to make money:

| | [put-spread](strategies/put-spread.toml) | [iron-condor](strategies/iron-condor.toml) |
|---|---|---|
| Opens | a bull put spread, $5 wide, short put near 0.25 delta | an iron condor, $5 wings, short strikes near 0.16 delta |
| Expiry | about 38 days out (30-45) | about 38 days out (30-45) |
| Only for | a credit of at least $0.55, a hard limit | a credit of at least $0.50, a hard limit |
| Closes | at 50% of the credit, at a loss of 2x the credit, or at 21 days | the same |
| Budget | €2,500 at risk, €500 per order, 6 opening orders a day | the same |
| Runs | :00 and :30, 10:00-15:30 New York | :15 and :45, 10:15-15:45 New York |

Both are defined risk, so the worst loss of every order is exact, and both
sell puts on the same underlyings at the same expiry, so they compete for
contracts: that tests the rule that a contract belongs to one strategy.

Each agent is a DurableAgent: instructions and an LLM, no code. It reaches the
broker only through the MCP tools it is granted, and it only runs when its cron
binding fires or someone starts a run by hand. Each run gets a fresh thread and
starts from what the broker says is open, so a missed or failed tick costs
nothing but time. The agents share one run procedure; a strategy's rules live
in its file in [strategies/](strategies), next to its limits, and reach its
agent through `get_strategy`.

The cron bindings need a controlplane change that has not been released yet
(`POST /v1/runs` on managed agents, and `cron` as a supported input binding).
Until it is, everything else here works and the agents run only by hand.

## How strategies share one account

IBKR keeps one set of positions for the account and knows nothing of
strategies, so the adapter keeps that apart itself:

- **The token says who is calling.** Each strategy's MCPServer sends that
  strategy's token. The adapter shows a strategy only its own orders and
  positions, and checks its orders against its own limits as well as the
  account's. The admin token is the desk: it sees everything.
- **Every order carries its strategy**, in its IBKR `orderRef`, so who placed
  what survives a restart.
- **A ledger records whose fills are whose** (SQLite, on the volume in
  [pvc.yaml](pvc.yaml)). A strategy's positions are the sum of its own fills,
  and its budget is the worst loss of those positions and its working
  opening orders.
- **A contract belongs to one strategy at a time.** An opening order on a
  contract another strategy holds or has an order working on is refused.
  Otherwise one strategy's trade would net against another's at IBKR and
  neither would know what it holds.
- **A close must reduce both** what the strategy holds by the ledger and what
  the account holds at IBKR.
- **The ledger is checked against IBKR** on every opening order, and when they
  disagree the strategies stop opening; see
  [When the ledger and IBKR disagree](#when-the-ledger-and-ibkr-disagree).

## What holds, and what does not

- **The hard limits hold.** The adapter refuses any order that opens risk past
  [limits.env](limits.env), the whole account's: allowed underlyings, max loss
  per order, total open risk, opening orders per day, daily loss, days to
  expiry, and a kill switch. It also refuses any past the strategy's own, in
  its file: budget, max loss per order, opening orders per day, days to
  expiry, which rights and how many legs, and a kill switch. Money limits are
  in euros, at IBKR's own EUR/USD rate. Closing orders always go through. How
  each is worked out is in
  [broker-ibkr/README.md](broker-ibkr/README.md#the-hard-limits).
- **The rules do not.** The run procedure and each strategy's `rules` are what
  the model is asked to do. A model can misread a quote, misjudge a price,
  or ignore a rule, within whatever the limits leave open. Size the limits
  for that, not for the strategy working.
- **Paper only.** The adapter will not place an order on an account that is not
  paper unless `IBKR_ALLOW_LIVE=true`, which nothing here sets.

## Setup, in order

### 1. The IBKR account

Options permission, paper account and market data:
[broker-ibkr/README.md](broker-ibkr/README.md#1-set-up-the-ibkr-account).

### 2. The Gateway image and the dependency lock

Both are what stands between the IBKR login and code nobody here has read.

- **The IB Gateway image** is built from IBKR's installer and an IBC release,
  each checked against a recorded hash, and pushed to `registry.88288338.xyz`.
  Follow `ansible-local-network/services/ib-gateway/README.md`, then put the
  image reference it prints, digest included, in `deployment.yaml`.
- **`broker-ibkr/requirements.lock`** pins every Python package the adapter
  installs, with hashes. Generate it as in
  [broker-ibkr/README.md](broker-ibkr/README.md#dependencies) and read the
  diff. Kustomize refuses to build until it exists.

### 3. The sealed secret

Four values: the **paper** username and password, a bearer token for the
desk (the admin), and the strategies' tokens. Use the paper account's own
username, never your live one: IBKR also accepts the live login in paper mode,
and that would put your real login in the cluster. From the repo root, on a
machine that reaches the cluster:

```bash
read -r TWS_USERID
read -rs TWS_PASSWORD
BROKER_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic options-agent --namespace options-agent --dry-run=client -o yaml --from-literal=TWS_USERID="$TWS_USERID" --from-literal=TWS_PASSWORD="$TWS_PASSWORD" --from-literal=BROKER_TOKEN="$BROKER_TOKEN" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > options-agent/sealed-secret.yaml
```

Keep `BROKER_TOKEN` in vaultwarden (step 6 needs it), then
`unset TWS_USERID TWS_PASSWORD BROKER_TOKEN`.

`STRATEGY_TOKENS` holds one token per strategy,
`<id>=<token>,<id>=<token>`. Add it to the sealed secret without retyping
the rest: `--merge-into` replaces only the keys given. Every strategy's token
goes in at once, since the key is replaced whole:

```bash
PUT_SPREAD_TOKEN=$(openssl rand -hex 32)
IRON_CONDOR_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic options-agent --namespace options-agent --dry-run=client -o yaml --from-literal=STRATEGY_TOKENS="put-spread=$PUT_SPREAD_TOKEN,iron-condor=$IRON_CONDOR_TOKEN" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml --merge-into options-agent/sealed-secret.yaml
```

Keep both tokens in vaultwarden (step 6 needs them too). Until the key is in
the secret, only the desk's token works and no strategy can call the broker.

### 4. The limits

[limits.env](limits.env) holds the account's limits, every strategy's
together; [strategies/](strategies) holds each strategy's own. Until every
account limit has a value the adapter opens nothing, which is also a safe
state to deploy in. `LIMIT_KILL_SWITCH=true` stops every strategy opening;
`kill_switch = true` in one file stops that strategy.

### 5. The Argo CD Application

The Application and the AppProject destination live in `ansible-local-network`
(`manifests/options-agent-app.yaml`, the `options-agent` destination in
`manifests/project.yaml`, a task in `argocd.yaml`). The source path is
`options-agent`; Argo CD finds `kustomization.yaml` and builds it with
Kustomize. Push this side first, with the sealed secret and
`requirements.lock`, since Kustomize refuses to build while either is missing.
Then from `ansible-local-network`:

```bash
kubectl apply -f playbooks/kubernetes/cluster/argocd/manifests/project.yaml -f playbooks/kubernetes/cluster/argocd/manifests/options-agent-app.yaml
```

Then watch the Gateway log in. It takes a minute or so; until then the adapter
logs `connecting to IB Gateway ... failed` every 5 seconds, then
`connected: account=DU... paper=True orders_enabled=True`. `/v1/health` then
shows `eur_per_usd`; while it is null, nothing opens. IBKR sends that rate
only for a currency the account holds, and a new euro paper account holds
only euros: convert a few hundred to dollars in it once, in IBKR's app, and
the rate appears within minutes. IBKR gives a paper account no forex quotes
over the API, so the adapter cannot fetch the rate any other way.

```bash
kubectl -n options-agent logs deploy/broker-ibkr -c ib-gateway -f
kubectl -n options-agent logs deploy/broker-ibkr -c broker -f
```

### 6. The MCP servers, the agents and the grants

One MCPServer per strategy, each sending that strategy's token, and one for
the desk, sending the admin token. Every agent's App ID must also be in the
scopes of the `llm-openai` component.

```bash
read -rs BROKER_TOKEN
read -rs PUT_SPREAD_TOKEN
read -rs IRON_CONDOR_TOKEN
diagrid mcpserver create broker --project test --url http://broker.options-agent.svc.cluster.local:8080/mcp --header Authorization:"Bearer $BROKER_TOKEN" --description "IBKR paper options broker, the desk's view of every strategy" --wait
diagrid mcpserver create broker-put-spread --project test --url http://broker.options-agent.svc.cluster.local:8080/mcp --header Authorization:"Bearer $PUT_SPREAD_TOKEN" --description "IBKR paper options broker, as the put-spread strategy" --wait
diagrid mcpserver create broker-iron-condor --project test --url http://broker.options-agent.svc.cluster.local:8080/mcp --header Authorization:"Bearer $IRON_CONDOR_TOKEN" --description "IBKR paper options broker, as the iron-condor strategy" --wait
unset BROKER_TOKEN PUT_SPREAD_TOKEN IRON_CONDOR_TOKEN
diagrid mcpserver tools list broker --project test
diagrid apply --project test -f catalyst/options-trader.yaml
diagrid apply --project test -f catalyst/options-put-spread.yaml
diagrid apply --project test -f catalyst/options-iron-condor.yaml
diagrid mcpserver access grant broker --project test --caller options-trader --allow-tools get_status,list_strategies,get_account,get_positions,get_quotes,get_option_chain,get_order,list_open_orders
diagrid mcpserver access grant broker-put-spread --project test --caller options-put-spread --allow-tools get_status,get_strategy,get_account,get_positions,get_quotes,get_option_chain,preview_order,place_order,get_order,list_open_orders,cancel_order
diagrid mcpserver access grant broker-iron-condor --project test --caller options-iron-condor --allow-tools get_status,get_strategy,get_account,get_positions,get_quotes,get_option_chain,preview_order,place_order,get_order,list_open_orders,cancel_order
```

The grants name every tool instead of `'*'`, so a tool added to the adapter
later is not granted by default. The desk gets only the read tools: it could
not trade if it tried. Each strategy's agent is granted only its own
MCPServer, so it can only ever be that strategy.

### 7. One run of each by hand

The same message a cron tick sends, in a new conversation:

```bash
diagrid managed-agent chat -m "Carry out your instructions." --agent options-put-spread --project test
diagrid managed-agent chat -m "Carry out your instructions." --agent options-iron-condor --project test
```

Each should check status, its strategy, orders and positions, follow its
rules, and report. `diagrid managed-agent chat list --agent options-put-spread
--project test` lists every run, the scheduled ones included. Ask the desk
what they did:

```bash
diagrid managed-agent chat -m "What does each strategy hold, and how much of its budget is left?" --agent options-trader --project test
```

### 8. Let them run

Run each by hand a few times as in step 7 and read the reports. Once the
controlplane change is out, start the clocks:

```bash
diagrid apply --project test -f catalyst/options-put-spread-schedule.yaml
diagrid apply --project test -f catalyst/options-iron-condor-schedule.yaml
```

Each tick is one agent run with a dozen or so tool calls: 12 a day per
strategy, every weekday. Nothing stops two runs of one strategy overlapping if
one runs past the next tick; the hard limits are the backstop.

## Adding a strategy

1. Write `strategies/<id>.toml`, copying an existing one: the id is the file
   name, lowercase letters, digits and dashes. Add it to the
   `options-agent-strategies` generator in
   [kustomization.yaml](kustomization.yaml), and make room for its budget and
   daily orders in [limits.env](limits.env).
2. Merge a token for it into the sealed secret as in step 3. `STRATEGY_TOKENS`
   is replaced whole, so give every strategy's token again.
3. Copy `catalyst/options-put-spread.yaml` and its schedule to
   `options-<id>.yaml` and `options-<id>-schedule.yaml`, replacing the id.
   Stagger the schedule's minutes against the others.
4. Commit and push; the pod rolls for the new token.
5. Add `options-<id>` to the `llm-openai` component's scopes, then create its
   MCPServer, apply its agent and grant it, as in step 6.

## When the ledger and IBKR disagree

The ledger only sees fills of the adapter's own orders, so it can fall out of
step with IBKR: an early assignment, a trade made in IBKR's own app, or a fill
while the adapter was down that IBKR no longer reports when it comes back,
since IBKR reports the current day's. Then:

- **A strategy holds a contract and IBKR's position differs:** that strategy
  opens nothing.
- **IBKR holds a live option no one in the ledger accounts for:** no strategy
  opens anything, since it may be one whose fills were missed.

Closes still go through, and `get_strategy` and `list_strategies` say why a
strategy is blocked. To see the difference and fix it, with the port-forward
below and the admin token:

```bash
curl -s localhost:8090/v1/ledger -H "Authorization: Bearer $BROKER_TOKEN"
curl -s localhost:8090/v1/ledger/adjustments -H "Authorization: Bearer $BROKER_TOKEN" -H 'content-type: application/json' -d '{"strategy": "put-spread", "symbol": "SPY261218P00450000", "quantity": -1, "price": 1.80}'
```

An adjustment is a fill entered by hand, in contracts, positive to add a long
or take off a short, at a price per share. Give a position to whoever opened
it, `desk` included, or take one away that IBKR no longer holds (price 0 for
an assignment). Options past their expiry leave the ledger by themselves.

## Restarting the broker

Until the Catalyst fix for re-attaching MCP servers ships, every restart of
the broker pod leaves each agent holding dead tools: each call fails with
`ClosedResourceError`, which the agents report as the broker being
unavailable. A change to the adapter's code, `limits.env` or the sealed secret
restarts the pod; a change to a strategy file does not. After a restart,
restart every agent that uses the broker: delete the pod of its sidecar
Deployment, `sidecar-test-<...>` in namespace `prj-14384718`.

## Stopping it

- **No new risk, any strategy:** `LIMIT_KILL_SWITCH=true` in `limits.env`,
  committed. The pod rolls and every opening order is refused; closes still
  go through.
- **No new risk, one strategy:** `kill_switch = true` in its file, committed.
  No restart; it takes effect a minute or two after Argo CD syncs.
- **No more runs:** `diagrid component delete options-<id>-schedule --project test`.
  It takes effect at once; apply the file again to restart the clock.
- **Working orders:** cancel them by id through the REST API, with the
  port-forward below, adding `?strategy=<id>` when two strategies used the
  same id. Positions stay open until closed.

To call the adapter by hand:
`kubectl -n options-agent port-forward svc/broker 8090:8080`, then curl as in
[the first paper session](broker-ibkr/README.md#first-paper-session) with
`-H "Authorization: Bearer $BROKER_TOKEN"`. With the admin token you see every
strategy; add `?strategy=<id>` to act as one, or `?strategy=desk` for orders
of your own.

## What is exposed

Only the adapter, through Service `broker`, and every call but `/healthz`
needs a bearer token, which only the MCPServers hold.

IB Gateway's API is not exposed. It has no authentication and bypasses the
adapter's limits, so it answers on the pod's loopback only; IBC refuses any
other connection (`AcceptIncomingConnectionAction=reject`). Flannel does not
enforce NetworkPolicy, so that refusal, not the network, is what keeps other
pods out.

Two things inside the pod hold the means to trade: the `ib-gateway` container
(the login) and the `broker` container (the Gateway's API). Both are built from
pinned, hash-checked parts; keep it that way when bumping.

## Bumping

- **The Gateway image:** `ansible-local-network/services/ib-gateway/README.md`.
  IBKR stops accepting logins from old Gateway versions after some months, so
  expect to do this now and then.
- **Python dependencies:** change `broker-ibkr/requirements.txt`, regenerate
  `requirements.lock`, and read its diff before committing. After an `mcp`
  bump, re-run `tools list` and diff it against the grant.
- **Code and limits:** a change to `broker-ibkr/broker_ibkr/` or `limits.env`
  rolls the pod by itself (see [Restarting the broker](#restarting-the-broker)).
  A new module must be added to `files` in `kustomization.yaml`. A change to
  `strategies/` does not roll it.

## Going live

More than flipping `TRADING_MODE`: also `IBKR_ALLOW_LIVE=true`, someone to
approve IBKR's two-factor prompt at every full login (roughly weekly, and on
every pod start), and limits sized for money you can lose.
