# options-agent

A Catalyst managed agent trading US options on an Interactive Brokers **paper**
account, on its own, every half hour of the US session.

```
cron binding        10:00-15:30 New York, weekdays              ../catalyst/options-trader-schedule.yaml
  -> POST /v1/runs  a run of options-trader, in a new thread    ../catalyst/options-trader.yaml
     -> MCPServer   broker, granted to options-trader
        -> broker   the adapter and its hard limits              broker-ibkr/, limits.env
           -> IB Gateway, same pod -> IBKR paper account        deployment.yaml
```

The agent is a DurableAgent: instructions and an LLM, no code. It reaches the
broker only through the MCP tools it is granted, and it only runs when its cron
binding fires or someone starts a run by hand. Each run gets a fresh thread and
starts from what the broker says is open, so a missed or failed tick costs
nothing but time.

The cron binding needs a controlplane change that has not been released yet
(`POST /v1/runs` on managed agents, and `cron` as a supported input binding).
Until it is, everything else here works and the agent runs only by hand.

## What holds, and what does not

- **The hard limits hold.** The adapter refuses any order that opens risk past
  [limits.env](limits.env): allowed underlyings, max loss per order, total open
  risk, opening orders per day, daily loss, days to expiry, and a kill switch.
  Closing orders always go through. How each is worked out is in
  [broker-ibkr/README.md](broker-ibkr/README.md#the-hard-limits).
- **The instructions do not.** They are what the model is asked to do: the run
  procedure, and your strategy. A model can misread a quote, misjudge a price,
  or ignore an instruction, within whatever the limits leave open. Size the
  limits for that, not for the strategy working.
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

Three values: the **paper** username and password, and a new bearer token for
the broker. Use the paper account's own username, never your live one: IBKR
also accepts the live login in paper mode, and that would put your real login
in the cluster. From the repo root, on a machine that reaches the cluster:

```bash
read -r TWS_USERID
read -rs TWS_PASSWORD
BROKER_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic options-agent --namespace options-agent --dry-run=client -o yaml --from-literal=TWS_USERID="$TWS_USERID" --from-literal=TWS_PASSWORD="$TWS_PASSWORD" --from-literal=BROKER_TOKEN="$BROKER_TOKEN" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > options-agent/sealed-secret.yaml
```

Keep `BROKER_TOKEN` in vaultwarden (step 6 needs it), then
`unset TWS_USERID TWS_PASSWORD BROKER_TOKEN`.

### 4. The limits

Write your numbers into [limits.env](limits.env) and leave
`LIMIT_KILL_SWITCH=true` for now. Until every limit has a value the adapter
opens nothing, which is also a safe state to deploy in.

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
`connected: account=DU... paper=True orders_enabled=True`.

```bash
kubectl -n options-agent logs deploy/broker-ibkr -c ib-gateway -f
kubectl -n options-agent logs deploy/broker-ibkr -c broker -f
```

### 6. The MCP server, the agent and the grant

```bash
read -rs BROKER_TOKEN
diagrid mcpserver create broker --project test --url http://broker.options-agent.svc.cluster.local:8080/mcp --header Authorization:"Bearer $BROKER_TOKEN" --description "IBKR paper options broker, with hard limits" --wait
diagrid mcpserver tools list broker --project test
diagrid apply --project test -f catalyst/options-trader.yaml
diagrid mcpserver access grant broker --project test --caller options-trader --allow-tools get_status,get_account,get_positions,get_quotes,get_option_chain,preview_order,place_order,get_order,list_open_orders,cancel_order
unset BROKER_TOKEN
```

The grant names all ten tools instead of `'*'`, so a tool added to the adapter
later is not granted by default.

### 7. One run by hand

The same message a cron tick sends, in a new conversation:

```bash
diagrid managed-agent chat -m "Carry out your instructions." --agent options-trader --project test
```

With the placeholder strategy the agent should check status, orders and
positions, open nothing, and report. `diagrid managed-agent chat list --agent
options-trader --project test` lists every run, the scheduled ones included.

### 8. The strategy, then let it run

1. Replace the last instruction in
   [catalyst/options-trader.yaml](../catalyst/options-trader.yaml) with your
   strategy, and `diagrid apply` it again.
2. Run it by hand a few times as in step 7 and read the reports.
3. Set `LIMIT_KILL_SWITCH=false` in `limits.env` and commit.
4. Once the controlplane change is out, start the clock:
   `diagrid apply --project test -f catalyst/options-trader-schedule.yaml`.

Each tick is one agent run with a dozen or so tool calls: 12 a day, every
weekday. Nothing stops two runs overlapping if one runs past the next tick;
the hard limits are the backstop.

## Stopping it

- **No new risk:** `LIMIT_KILL_SWITCH=true` in `limits.env`, committed. The pod
  rolls and every opening order is refused; closes still go through.
- **No more runs:** `diagrid component delete options-trader-schedule --project test`.
  It takes effect at once; apply the file again to restart the clock.
- **Working orders:** cancel them by id through the REST API, with the
  port-forward below. Positions stay open until closed.

To call the adapter by hand:
`kubectl -n options-agent port-forward svc/broker 8090:8080`, then curl as in
[the first paper session](broker-ibkr/README.md#first-paper-session) with
`-H "Authorization: Bearer $BROKER_TOKEN"`.

## What is exposed

Only the adapter, through Service `broker`, and every call but `/healthz`
needs the bearer token, which only the MCPServer holds.

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
  rolls the pod by itself. A new module must be added to `files` in
  `kustomization.yaml`.

## Going live

More than flipping `TRADING_MODE`: also `IBKR_ALLOW_LIVE=true`, someone to
approve IBKR's two-factor prompt at every full login (roughly weekly, and on
every pod start), and limits sized for money you can lose.
