# telegram-bridge

The trigger for `telegram-agent`. Watches Luis's **Saved Messages** chat and
starts an agent run for anything he writes there. Ask for milk in that chat and
the answer comes back asking which one -- telegram-agent fields the message and
hands the groceries to `shopping-agent` as a teammate, then relays its reply.

It used to invoke `shopping-agent` directly. It no longer does, because
telegram-agent is the only agent granted Telegram tools: a run against
shopping-agent would now wake an agent with no way to answer in the chat.

~230 lines of standard-library Python in a ConfigMap, on `python:3.12-slim`.
Nothing to build, nothing to install at start, no registry needed to come up.

## Why a bridge at all, and why polling

A Catalyst DurableAgent answers invocations. It is not a daemon, so nothing
re-enters a wait and nothing notices a Telegram message by itself. Something
has to watch the chat and invoke -- and that is what telegram-mcp's event tools
are for. Upstream calls them tools "for integrators that follow chats (such as
chat bridges)".

But not the long-poll ones. `wait_for_new_message` and
`wait_for_settled_message` block on **incoming** messages from other people,
and telegram-mcp is signed in **as Luis**, so everything he writes is
*outgoing* and would never wake them. `get_messages_after` does return own
messages (`out`), and is documented as the tool "for integrators that resume
from the last message they saw". So the bridge keeps a cursor and polls it
every 5s -- one cheap call, no long-held streams.

That has a security property worth keeping: **only Luis's own Saved Messages
can drive the agent.** Nobody who messages his account reaches it. If this is
ever pointed at another chat, that goes away and the sender becomes an
untrusted input to an agent that can write to a cart.

## The loop it must not get into

Luis and the agent are the *same* Telegram account in this chat, so the agent's
own replies are outgoing too, and nothing in a message's metadata distinguishes
them. Left alone the bridge would read the agent's reply as a new instruction,
forever.

Two defences:

1. The agent is instructed to begin every Telegram message with 🛒, and the
   bridge skips anything starting with it. `AGENT_MARKER` here and the
   instruction in `catalyst/telegram-agent.yaml` **must agree** -- change one
   and change the other. It stays the shopping cart even though telegram-agent
   answers about anything: it is a loop marker, not a topic, and a prettier one
   would buy nothing for the risk of the two drifting apart.
2. `MAX_RUNS_PER_WINDOW` (8 per 120s). If the marker ever fails, this stops it
   running away before someone reads the log.

## Flow

```
Luis types in Saved Messages
  → bridge:    get_messages_after(chat_id=me, after_id=cursor)  [telegram-mcp, direct]
  → bridge:    POST /v1.0/invoke/telegram-agent/method/v1/threads/<id>/runs
  → tg-agent:  recognises groceries, calls the shopping-agent teammate tool
  → shopping:  get_most_bought → returns "which milk? 1/2/3" to tg-agent
  → tg-agent:  send_message "🛒 which milk? ..."
  → Luis answers in the same chat
  → bridge:    dispatches it to the SAME thread, so tg-agent still has the question
  → tg-agent:  calls shopping-agent again, passing BOTH the options and his choice
  → shopping:  search_products → add_to_cart → get_cart → close_session → reports back
  → tg-agent:  send_message "🛒 added ..., total ..."
```

The teammate tool is not something either agent declares. A DurableAgent's spec
is only `goal`, `instructions`, `llmConfig` and `role`; Catalyst puts every
other agent in the project into each agent's tool list itself, and injects the
instructions that say so. `diagrid agent registry get telegram-agent --project
test` shows what it actually ends up with.

One agent thread per chat, kept on the volume. Without it, "2" in answer to
"which milk?" arrives with no idea what the options were.

## State

`/state/state.json` on Longhorn, two fields:

- `last_id` -- the last message already handled. On first boot the bridge sets
  it to the newest message **without dispatching**, so it does not replay
  whatever is already in Saved Messages as a pile of shopping commands.
- `thread_id` -- the agent thread, so context survives a restart.

To make it forget and start fresh from now:

```bash
kubectl -n telegram-bridge exec statefulset/telegram-bridge -- rm -f /state/state.json
```

## The sealed secret

Already in this repo. Both values were read out of the cluster and the control
plane -- no personal credential is involved -- so unlike `continente-mcp` this
one did not need creating by hand. To re-seal after rotating either:

```bash
TG=$(kubectl -n telegram-mcp get secret telegram-mcp-auth -o jsonpath='{.data.MCP_BEARER_TOKEN}' | base64 -d) && CA=$(diagrid appid get telegram-bridge --project test -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['apiToken'])") && kubectl create secret generic telegram-bridge-auth --namespace telegram-bridge --dry-run=client -o yaml --from-literal=TELEGRAM_MCP_TOKEN="$TG" --from-literal=CATALYST_API_TOKEN="$CA" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > telegram-bridge/sealed-secret.yaml && unset TG CA
```

`TELEGRAM_MCP_TOKEN` is a copy of `telegram-mcp-auth`'s `MCP_BEARER_TOKEN`:
secrets do not cross namespaces, so rotating one means rotating both.

## Before the first sync

The Application and the AppProject destination live in `ansible-local-network`
(`manifests/telegram-bridge-app.yaml`, the `telegram-bridge` destination in
`manifests/project.yaml`, a task in `argocd.yaml`).

## Changing bridge.py

A ConfigMap change does not roll the pod. Bump `checksum/src` in
`statefulset.yaml` in the same commit, or the old code keeps running against
the new ConfigMap.

## Reading the log

`run completed` means the agent finished. `run for <id> ended 'failed'` means it
did not, and the bridge found that out by asking the thread rather than by
trusting the POST -- posting a run answers 200 as soon as it is accepted, and a
run whose LLM is unreachable answers 200 and then fails. An earlier version
logged that as success, which is worth remembering before trusting any
"finished" line in a tool like this.

## Watching it

```bash
kubectl -n telegram-bridge logs -f statefulset/telegram-bridge
```

It listens on nothing and has no probes -- a crash shows up as restarts, a
wedged poll as silence.

## Risks this does not solve

- It can invoke an agent that writes to the Continente cart, and it holds a
  token that reads Telegram. Its own namespace and secret for that reason.
- The marker is a convention, not a boundary. A sufficiently confused agent
  that drops the 🛒 gets its own words back as instructions, until the rate
  limit trips.
- The cursor advances *before* the run, so a message that makes the agent fail
  is dropped rather than retried. Deliberate -- the alternative is a poison
  message re-invoking the agent forever -- but it means a failed run needs
  re-asking. The log says which runs ended badly; those are the ones to re-ask.
- `get_messages_after` is granted to nothing in Catalyst; the bridge talks to
  telegram-mcp directly with the bearer token. So Catalyst's tool grants do not
  constrain the bridge, only the agent. The server's `TELEGRAM_EXPOSED_TOOLS`
  still does. This is also why "telegram-agent is the only caller of the
  telegram MCP" is true of Catalyst callers and not of the cluster: the bridge
  has to read the chat to know there is anything to wake an agent for, and a
  DurableAgent cannot poll on its own.
