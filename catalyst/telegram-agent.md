# telegram-agent

The agent `telegram-bridge` wakes, and the only one on the team holding
Telegram tools. It reads what Luis writes in Saved Messages, answers him, and
hands work it should not do itself to the teammate whose job it is.

Applied with `diagrid apply --project test -f catalyst/telegram-agent.yaml`.

## Why it is separate from shopping-agent

Before this, the bridge woke `shopping-agent` directly and that agent both did
the shopping and talked to Luis. Two reasons to split it:

- **One holder of the Telegram tools.** `send_message` is the one tool on that
  server that can put text in front of a person, and the read tools reach the
  whole account, not just this chat. Keeping them with a single agent means
  there is one place to reason about what can be said and to whom, rather than
  one grant per agent that ever wants to talk to Luis.
- **The next trigger.** Anything else that should reach Luis on Telegram -- a
  eufy alert, a healthcheck summary -- can be a teammate that returns text,
  instead of each one growing its own send grant and its own marker discipline.

## How delegation actually works

There is no field for it. A DurableAgent's spec is only `goal`,
`instructions`, `llmConfig` and `role` -- no tools list, no sub-agents, no
registry reference. Tools arrive from two places, neither of them this file:

1. **MCP grants to its AppID** (`diagrid mcpserver access grant`).
2. **Team membership.** Catalyst puts every other agent in the project into
   each agent's tool list by itself, and injects the instructions that explain
   it. Read them back with:

```bash
diagrid agent registry get telegram-agent --project test
```

The injected text says, verbatim, that "each teammate appears in your tool list
as a callable tool", that `@<name>` in the user's message means delegate to
that agent, and that it must never call a tool targeting itself. So the
instructions in the yaml do not need to teach delegation -- only *when* to use
it, and what to pass.

That also means **every agent in project `test` is reachable from every other
one.** Adding an agent adds a tool to all of them. There is no per-pair switch;
`diagrid agent access` manages `kind: Agent` registry entries (external A2A
endpoints), not these.

## The two things that break if you edit the instructions carelessly

**The marker.** Luis and the agent are the same Telegram account in Saved
Messages, so the bridge cannot tell their messages apart except by the 🛒
prefix, which it skips. Every outbound message needs it, whatever the topic --
it is a loop marker, not a label for shopping. `AGENT_MARKER` in
`telegram-bridge/statefulset.yaml` and the instruction here must agree, in the
same commit. If they drift, the agent reads its own reply back as a new
instruction from Luis and loops until `MAX_RUNS_PER_WINDOW` trips.

**The teammate has no memory of the chat.** telegram-agent keeps one durable
thread per chat, so *it* remembers the conversation. A teammate it calls does
not: it sees one task and answers it. So when shopping-agent has asked which of
three milks Luis meant and he replies "2", telegram-agent has to send the
options *and* the choice. "He chose 2" alone is a task shopping-agent cannot
do, and the failure mode is it guessing. Both sides are instructed for this --
telegram-agent to pass the context, shopping-agent to refuse rather than
re-derive a list and assume the order came out the same.

## Grants

telegram-agent is the only Catalyst caller of the `telegram` MCP server. Not
the only caller in the cluster: the bridge reads the chat directly with the
nginx bearer token, because a DurableAgent cannot poll and something has to
notice there is a message at all. See `telegram-bridge/README.md`.

```bash
diagrid mcpserver access get telegram --project test
diagrid mcpserver access get continente --project test
```

`telegram` should list telegram-agent and nothing else. telegram-agent has no
cart tools and shopping-agent has no Telegram tools, which is the whole point of
the split.

`continente` is not part of that change and still lists `agent1` with a read
subset (`search_products`, `get_favorites`, `get_cart`) alongside
shopping-agent's full set. Harmless -- it reads a grocery catalogue -- but it is
the same loose end `telegram` had, so revoke it when tidying:

```bash
diagrid mcpserver access revoke continente --project test --caller agent1 --all
```

## Verifying it end to end

The bridge only logs when it dispatches, so silence means nothing new in the
chat.

```bash
kubectl -n telegram-bridge logs -f statefulset/telegram-bridge
```

`run completed` means the agent finished -- the bridge asks the thread rather
than trusting the POST, because a run whose LLM is unreachable answers 200 and
then fails. For a delegation round trip, watch both MCP servers: a grocery
message should produce `telegram` calls from telegram-agent's sidecar and
`continente` calls from shopping-agent's, and the reply to Luis should start
with 🛒.
