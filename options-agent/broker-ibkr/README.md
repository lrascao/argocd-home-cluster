# broker-ibkr

The broker for the options agents, implemented for Interactive Brokers: an
MCP server the Catalyst managed agents trade through, the same calls as a REST
API, the ledger of which strategy holds what, and the hard limits on what may
be opened. It talks to an IB Gateway running next to it. **Orders go only to a
paper account** (IBKR paper account ids start with `DU`) unless
`IBKR_ALLOW_LIVE=true`.

The agents only ever see the tools below ([models.py](broker_ibkr/models.py)),
so moving to another broker means writing another adapter that serves the same
tools.

How it is deployed and how the agent is set up is in
[../README.md](../README.md). This file is the adapter itself, and running it
on your machine against a desktop Gateway.

## 1. Set up the IBKR account

Menu names are approximate; IBKR moves them around.

- **Options permission.** Client Portal → Settings → Trading Permissions →
  Options → United States. As an EU client you answer a knowledge and
  experience questionnaire first. The paper account copies the live account's
  permissions, so it can trade options only once the live account can.
- **Paper account.** Settings → Paper Trading Account. It has its own username;
  that is the one IB Gateway logs in with.
- **Market data.** US options quotes need the OPRA subscription. Without it,
  leave `IBKR_MARKET_DATA_TYPE=3` (delayed quotes). With it, set the same page's
  option to share live subscriptions with the paper account, and set
  `IBKR_MARKET_DATA_TYPE=1`.

## 2. Run IB Gateway on your machine

In the cluster the `ib-gateway` container does all of this through its
environment; this section is for a desktop Gateway.

Install IB Gateway (stable), choose **IB API** and **Paper Trading**, and log in
with the paper username. Then Configure → Settings → API → Settings:

- untick **Read-Only API**, or every order is refused
- socket port **4002** (the paper default; live is 4001)
- keep **Allow connections from localhost only** ticked

Under Lock and Exit choose **Auto restart**. The Gateway restarts once a day and
the adapter reconnects on its own. About once a week IBKR wants a full login
again, and until you log in, `get_status` reports `connected: false`.

## 3. Run the adapter

```bash
cd options-agent/broker-ibkr
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
uvicorn broker_ibkr.app:app --host 127.0.0.1 --port 8090 --loop asyncio
```

Settings come from the environment:

| Variable | Default | |
|---|---|---|
| `IBKR_HOST` / `IBKR_PORT` | `127.0.0.1` / `4002` | where IB Gateway listens |
| `IBKR_CLIENT_ID` | `17` | keep it fixed: IBKR ties orders to the client id that placed them |
| `IBKR_ACCOUNT` | the only one | needed only if the login manages several accounts |
| `IBKR_MARKET_DATA_TYPE` | `3` | `1` live, `3` delayed |
| `IBKR_ALLOW_LIVE` | unset | `true` lets it place orders on a non-paper account |
| `BROKER_TOKEN` | unset | the desk's (admin's) token; when set, every call but `/healthz` needs `Authorization: Bearer <token>` or a strategy's |
| `STRATEGY_TOKENS` | unset | `<id>=<token>,...`: each strategy's token |
| `STRATEGIES_DIR` | unset | where the strategy files are; unset, no strategy can open anything |
| `LEDGER_PATH` | `ledger.db` | the SQLite ledger |
| `LIMIT_*` | unset | the account's hard limits, described in [../limits.env](../limits.env); until all are set, nothing opens |

To try it as a strategy on a laptop, leave `BROKER_TOKEN` unset (every other
call is then the desk), set `STRATEGIES_DIR=../strategies` and
`STRATEGY_TOKENS=put-spread=test`, and send `Authorization: Bearer test`.

## Dependencies

`requirements.txt` names what the adapter imports, pinned.
`requirements.lock` is what the cluster installs: every package, transitive
ones included, with its hashes, installed with `--require-hashes --no-deps`, so
anything that does not match is refused. Regenerate it whenever
`requirements.txt` changes, for the cluster's Python and platform, and read the
diff before committing:

```bash
cd options-agent/broker-ibkr
uv pip compile requirements.txt --generate-hashes --python-version 3.13 --python-platform x86_64-unknown-linux-gnu --output-file requirements.lock
```

`requirements-dev.txt` (the same pins plus pytest, no hashes) is for a laptop
only.

## Tools

The MCP server is at `/mcp`; each tool is also a REST call under `/v1`.

The token says who is calling ([strategies.py](broker_ibkr/strategies.py)). A
strategy's token sees and trades only that strategy's orders and positions.
The desk's token sees every strategy's, and on REST can act as one with
`?strategy=<id>`, or as itself with `?strategy=desk`.

| MCP tool | REST | |
|---|---|---|
| `get_status` | `GET /v1/health` | connected, account, paper, orders enabled, who the caller is, EUR/USD, and the account's hard limits |
| `get_strategy` | `GET /v1/strategies` | the caller's strategy: rules, limits, open risk, budget left, opening orders today, and why it is blocked, if it is |
| `list_strategies` | `GET /v1/strategies` | every strategy, for the desk |
| `get_account` | `GET /v1/account` | net liquidation, available funds, buying power, in the account's base currency |
| `get_positions` | `GET /v1/positions` | the caller's positions, by the ledger; the desk's view adds what IBKR holds that no one accounts for |
| `get_quotes` | `GET /v1/quotes?symbol=…` | 1–50 symbols; bid/ask/last/close, greeks for options |
| `get_option_chain` | `GET /v1/chains/{underlying}` | per OCC root: expirations and strikes (US stocks and ETFs, not indexes) |
| `preview_order` | `POST /v1/orders/preview` | opens or closes, max loss in USD and EUR, margin, commission, and why the limits would refuse it |
| `place_order` | `POST /v1/orders` | place, or return the order already placed under that `client_order_id` |
| `list_open_orders` | `GET /v1/orders` | the caller's orders still working |
| `get_order` | `GET /v1/orders/{client_order_id}` | one of the caller's orders |
| `cancel_order` | `DELETE /v1/orders/{client_order_id}` | cancel; always allowed |
| | `GET /v1/ledger` | desk only: each owner's holdings, what IBKR holds that no one accounts for, and who that blocks |
| | `POST /v1/ledger/adjustments` | desk only: a fill entered by hand, so the ledger matches IBKR |

FastAPI serves the REST schema at `/docs`. Conventions:

- **Options are OCC symbols**: root padded to six, `YYMMDD`, `C`/`P`, strike × 1000
  in eight digits. `SPY   261218P00450000` is the SPY 450 put of 2026-12-18.
- **`limit_price` is the net per share**: positive pays a debit, negative
  receives a credit. A spread is one combo order whose legs fill together or not
  at all, so it is never left half open.
- **Limit orders, good for the day.** There are no market orders.
- **`client_order_id` makes placing safe to retry.** One per order. Sending it
  again returns the existing order; a cancelled order keeps its id, so trying
  again needs a new one. Ids are per strategy: two strategies may use the same
  one.

## The hard limits

[limits.py](broker_ibkr/limits.py). They live here, not in the agents, because
a managed agent has no code of its own: its instructions are requests, and this
is the only place a refusal holds whatever the model does. Keep it that way. A
limit written only into the instructions is not a limit.

Two layers. The account's ([../limits.env](../limits.env)) apply to every
opening order, the desk's included. A strategy's own (its file in
[../strategies](../strategies)) apply to its orders on top: its budget, max
loss per order, opening orders per day, days to expiry, the rights and number
of legs an opening order has, and its own kill switch.

- **Opening or closing.** An order is a close when every leg trades against a
  position at least its size, both the caller's by the ledger and the
  account's at IBKR, and no other order is working that contract. A close is
  always allowed, kill switches included. Anything else opens risk and must
  pass every limit.
- **Euros.** Money limits are in euros. A loss is worked out in dollars from
  the legs and converted at IBKR's own rate: its `ExchangeRate` account
  values, which it sends only for currencies the account holds, and until
  the account holds dollars, its EUR.USD quote on IDEALPRO. With neither,
  nothing opens.
- **One owner per contract.** An opening order is refused on a contract
  another strategy holds or has an order working on, and, for a strategy, on
  one where IBKR holds a position no one in the ledger accounts for.
- **Max loss** is worked out at expiry, at the limit price, from the legs'
  payoff. That is only defined when all legs share an expiry, so an opening
  order must. Selling more calls than are bought is an unlimited loss and is
  refused.
- **Open risk** adds the max loss of every option position, grouped by
  underlying and expiry and priced at what it cost, to that of every working
  opening order. Grouping by expiry overstates a calendar, never understates.
  The account's is IBKR's positions; a strategy's (its budget) is its own
  positions by the ledger, at their average cost.
- **Opening orders per day** counts by UTC day, and counts orders that were
  refused by IBKR or cancelled. The count is kept in each order's IBKR
  `orderRef`, `o.<strategy>:<YYYYMMDD>:<client_order_id>` (no `.<strategy>`
  for the desk), so it survives a restart, and so does who placed what.
- **Daily loss** is IBKR's own daily P&L for the account, converted to euros.
  Until IBKR has sent it, nothing opens.

## The ledger

[ledger.py](broker_ibkr/ledger.py), SQLite at `LEDGER_PATH`. IBKR keeps one
set of positions for the account, so this is what says whose they are: every
option fill of the adapter's orders, under the strategy in its `orderRef`. A
strategy's positions are the sum of its fills, at IBKR's average-cost method.
IBKR reports the day's fills on connect and each new one as it happens; the
adapter records them every few seconds and before every order.

It is compared with IBKR's positions on every opening order. Where a strategy
holds a contract and IBKR's position differs, that strategy opens nothing.
Where IBKR holds a live option no one in the ledger accounts for, no strategy
opens anything, since it may be one whose fills were missed. Closes still go
through. `GET /v1/ledger` shows the difference and `POST
/v1/ledger/adjustments` fixes it ([../README.md](../README.md#when-the-ledger-and-ibkr-disagree)).
Options past their expiry leave the ledger by themselves: IBKR reports no
fill when an option expires.

## First paper session

None of this has run against IB Gateway yet. It is written against the
ib_async 2.1.0 and mcp 1.30.0 sources. For the test spread to pass the limits,
start the adapter with SPY allowed, a max loss per order of at least $875 (the
spread's), an expiry window that holds 2026-12-18, and the kill switch off.
Then, in order:

```bash
cat > /tmp/spread.json <<'EOF'
{"client_order_id": "test-1", "quantity": 1, "limit_price": -1.25,
 "legs": [{"symbol": "SPY261218P00450000", "side": "sell"},
          {"symbol": "SPY261218P00440000", "side": "buy"}]}
EOF
curl -s localhost:8090/v1/health
curl -s localhost:8090/v1/chains/SPY
curl -s -G localhost:8090/v1/quotes --data-urlencode symbol=SPY --data-urlencode symbol=SPY261218P00450000
curl -s localhost:8090/v1/orders/preview -H 'content-type: application/json' -d @/tmp/spread.json
curl -s localhost:8090/v1/orders -H 'content-type: application/json' -d @/tmp/spread.json
curl -s localhost:8090/v1/orders/test-1
curl -s -X DELETE localhost:8090/v1/orders/test-1
```

1. Health says `paper: true`, `orders_enabled: true`, `caller: desk`, a
   value for `eur_per_usd`, and shows the limits.
2. Quotes come back with prices. If delayed option quotes are empty without
   OPRA, that is the subscription, not the adapter. If a real option comes
   back as `no such contract`, the adapter's lookup by OCC symbol (IBKR's
   `localSymbol`) is what failed.
3. Preview says `opens: true`, `max_loss: 875`, `refused: null`, with a margin
   change and a commission.
4. The spread is far below the market, so it should sit unfilled. In IBKR's
   own app it must show as a **credit** of 1.25. That negative-price
   convention for combos is the thing most worth seeing with your own eyes.
5. Placing `test-1` again returns the same order instead of a second one, and
   still does after you restart the adapter.
6. Cancel moves it to `cancelled`.
