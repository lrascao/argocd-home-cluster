# shopping-agent

The Telegram → Continente shopping flow: ask for "milk" in a Telegram chat,
get asked back which of the milks you actually buy, answer, and it lands in
the cart.

Three parts, two of them built:

| Part | Where | State |
| --- | --- | --- |
| `continente` MCP server | `continente-mcp/`, ArgoCD | done |
| `telegram` MCP server | `telegram-mcp/`, ArgoCD | done, `read-only+send_message` |
| `shopping-agent` | `shopping-agent.yaml`, Catalyst | created, grants applied |
| the Telegram bridge | nothing yet | **not built** |

## The agent

`shopping-agent.yaml` is the source of truth. It is applied to Catalyst, not
synced by ArgoCD -- the control plane holds the agent and renders only the
AppIdentity, its sidecar and the MCPServer objects into `prj-14384718`:

```bash
diagrid apply --project test -f catalyst/shopping-agent.yaml
```

```bash
diagrid export --durableagent shopping-agent --project test
```

`diagrid apply` accepts `DurableAgent` even though the CLI has no subcommand
for it and `diagrid agent` manages something else entirely (an `Agent` is a
name and an endpoint, with no instructions, model or tools). Two things the
`--dry-run` will not tell you: `llmConfig.apiKey` is required on create, and
the `model` has to be the vllm Deployment's own `--served-model-name`.

> `agent1` points at `qwen3-32b-awq`, which the live vllm Deployment does not
> serve -- it serves `qwen3-5-35b-a3b-fp8`. `shopping-agent` uses the served
> name. agent1's looks stale and worth a check.

### Why the instructions are shaped the way they are

`search_products` ranks by the **favourites list only** -- `rankByPreference`
scores +100 for a favourite id and +50 for a name match, and never looks at
the order history. Measured on this account:

- `search_products("leite")` returns 105 products and puts **Leite Magro sem
  Lactose Nova Açores** first, because it happens to be favourited.
- The milk actually bought most, **Leite UHT Meio Gordo Nova Açores** (4
  units), is **not in the top 8 and not starred**.

An agent that searched and took the first hit would quietly buy lactose-free
skimmed milk. So the instructions make `get_most_bought` the signal and use
the search only afterwards, to turn a name into a `product_id`.

Two more sharp edges they work around:

- `get_most_bought` returns **names with no `product_id`**. The id has to come
  from a follow-up `search_products` on the full name.
- Only **one order** is visible on this account today, so "usually" means that
  order plus the 54 favourites, tallied by quantity within it rather than
  frequency across orders. It improves as orders accrue.

## Grants

Applied. The servers' own surfaces are the outer bound; these are the inner
one.

```bash
diagrid mcpserver access grant continente --project test --caller shopping-agent --allow-tools search_products,get_favorites,get_most_bought,get_cart,add_to_cart,update_cart_item,close_session
```

```bash
diagrid mcpserver access grant telegram --project test --caller shopping-agent --allow-tools get_me,get_chat,get_messages,get_message_context,send_message
```

`send_message` is granted here and **nowhere else** -- `agent1` keeps its
read-only Telegram grant on purpose. It already holds `whatsapp_send`, full
Telegram chat read and polymarket; adding cart writes and a Telegram send to
that same agent is what makes anything it reads into something it could be
talked into sending or buying. A separate caller keeps this to one chat and
one cart.

Deliberately left out:

| Not granted | Why |
| --- | --- |
| `refresh_favorites` | re-scrapes the favourites page; run by hand when the list changes, not on every trip |
| `get_order_history` | `get_most_bought` distils it; the raw history is every item ever bought |
| telegram `list_chats`, `get_history`, `search_global`, `list_contacts`, `search_contacts` | reach every chat and other people's details; this agent needs the one chat it is answering in |
| telegram `wait_for_*`, `get_messages_after` | the bridge polls, not the agent -- see below |

## The bridge, which does not exist yet

`wait_for_settled_message` blocks until a message arrives, but a DurableAgent
answers invocations; it is not a daemon, so nothing re-enters that wait. The
missing piece is a small long-poll service, which is what those tools were
built for -- upstream describes `wait_for_events` as being "for integrators
that follow chats (such as chat bridges)".

Shape:

1. Long-poll `wait_for_settled_message` against
   `telegram.telegram-mcp.svc.cluster.local:8080/mcp`, with the
   `telegram-mcp-auth` bearer token. Debounced, so a burst of typing arrives
   as one message.
2. On a message from Luis's own chat, and only his, invoke `shopping-agent`
   through Catalyst with the text.
3. Go back to waiting. The agent answers in Telegram itself via
   `send_message`, so the bridge never replies -- it only triggers, which
   keeps the reply path and the trigger path separate.

Notes for whoever builds it:

- The waits hold a stream open up to `TELEGRAM_TOOL_TIMEOUT_SECONDS` (55s) per
  call, so it is a loop of long calls, not a busy poll.
- It needs its own filter on sender: the agent's instructions refuse
  instructions from groups, channels and forwards, but the bridge should not
  wake it for them at all.
- It holds a Telegram token and can invoke an agent that writes to the cart.
  Its own namespace and sealed secret, one replica, like the rest.
- continente-mcp reaps an idle session after three minutes, so a slow
  conversation gets a fresh browser rather than a stale one. That is fine --
  the cookie jar persists, so no re-login.

## Current blocker, unrelated to any of this

The agent's LLM is down: ArgoCD shows `inference` **Degraded** and the `vllm`
pod `Pending`, because that GPU node is the cloud one that comes and goes. The
agent is created and granted but cannot think until vllm is back.
