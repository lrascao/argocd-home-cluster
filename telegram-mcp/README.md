# telegram-mcp

[chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) (Apache-2.0,
Telethon over MTProto) linked to Luis's personal Telegram account as a device,
serving MCP (streamable HTTP) at `/mcp` for the Catalyst agents. Pinned to tag
`v3.3.4`.

**Read-only.** `TELEGRAM_EXPOSED_TOOLS=read-only` keeps the 65 tools annotated
`readOnlyHint=True` out of the 147 registered and drops the other 82 -- every
`send_*`, `delete_*`, `edit_*`, `ban_*`, `join`/`leave`, the profile and
privacy setters, `forward_*` and `download_media`. That is the outer bound: the
Catalyst grant narrows it further, but a widened grant still cannot reach a
tool the server never registered.

One pod, two containers plus an init container. `mcp` runs the server with
`uvx` on the pod's loopback; `auth` is nginx on `:8080`, the only published
port, and refuses anything without `Authorization: Bearer <token>`. The server
has no auth of its own, and flannel does not enforce NetworkPolicy, so without
the proxy any pod in the cluster could read every chat.

Unlike WhatsApp, this is not an unofficial client: MTProto is a documented
Telegram API and third-party clients are expected. The account is not at risk
of a ban for being here.

## Four things about upstream that this deployment depends on

- **`TELEGRAM_SESSION_STRING` beats `TELEGRAM_SESSION_NAME`**, the opposite of
  what the upstream README says. `_discover_accounts()` in `runtime.py` tests
  the string first and only falls through to the file. So the string is scoped
  to the init container and must never be added to `mcp` -- with both set, the
  file session on the volume is silently ignored and every restart refetches
  the update backlog.
- **Not from PyPI.** The `telegram-mcp` name on PyPI belongs to an unrelated
  project. The install is a direct reference to the GitHub tag tarball, so
  github.com must be reachable whenever the pod restarts -- the same bargain
  polymarket-mcp makes with PyPI.
- **The package metadata lies.** `pyproject.toml` still reads `2.0.1` at tag
  `v3.3.4`; upstream does not bump it. `pip show` / `importlib.metadata` will
  say 2.0.1. Trust the tag in the manifest.
- **Host must stay nginx's default.** `MCP_ALLOWED_HOSTS=127.0.0.1:*` turns on
  the SDK's DNS-rebinding protection, and nginx forwards `Host: $proxy_host`
  (`127.0.0.1:8765`), which is what passes. Don't add `proxy_set_header Host` --
  every call would get `421 Invalid Host header`, the same trap as polymarket.

## Before the first sync

The Application and the AppProject destination live in `ansible-local-network`
(`manifests/telegram-mcp-app.yaml`, the `telegram-mcp` destination in
`manifests/project.yaml`, a task in `argocd.yaml`). Push this side first, with
the sealed secret, then from `ansible-local-network`:

```bash
kubectl apply -f playbooks/kubernetes/cluster/argocd/manifests/project.yaml -f playbooks/kubernetes/cluster/argocd/manifests/telegram-mcp-app.yaml
```

### Pre-flight the install

The `uvx --from "<name> @ <url>"` form was checked with pip's equivalent, not
with uv itself, so prove it on the cluster before the first sync rather than
reading a crash-looping init container. This should install and then exit with
`Error: No Telegram session configured`, which is the proof -- the tarball
resolved and the console script ran:

```bash
kubectl -n default run uvx-preflight --rm -it --restart=Never --image=ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim --env=HOME=/tmp --env=UV_CACHE_DIR=/tmp/cache --env=UV_PYTHON_DOWNLOADS=never --command -- uvx --exclude-newer 2026-11-05T00:00:00Z --from "telegram-mcp @ https://github.com/chigwell/telegram-mcp/archive/refs/tags/v3.3.4.tar.gz" telegram-mcp
```

If uv rejects that spelling, drop the `"<name> @ "` prefix and pass the URL to
`--from` on its own; the executable is named after the package either way.

## Get API credentials

Sign in at <https://my.telegram.org/apps> with the account's phone number and
create an application. It yields an `api_id` and an `api_hash`, tied to the
account, reusable forever. They are not a login on their own -- the session is.

## Generate the session string

On the **personal** Mac (this is an account credential, like `ca/` -- see the
two-Mac split). There is no `uv` here, so use a throwaway venv; the package
installs cleanly from the tag tarball:

```bash
python3 -m venv /tmp/tgsess && /tmp/tgsess/bin/pip -q install "telegram-mcp @ https://github.com/chigwell/telegram-mcp/archive/refs/tags/v3.3.4.tar.gz"
```

```bash
read -r TELEGRAM_API_ID; read -rs TELEGRAM_API_HASH; export TELEGRAM_API_ID TELEGRAM_API_HASH; /tmp/tgsess/bin/telegram-mcp-generate-session --qr
```

It prints a QR code; scan it from the phone under **Settings → Devices → Link
Desktop Device**. (`--phone` instead sends a code to Telegram.) The session
string is printed to stdout: it is a full login to the account, so put it
straight into vaultwarden and clear the scrollback. `rm -rf /tmp/tgsess` after.

## Create the sealed secret

One secret, four keys. The bearer token is hex only: it is pasted into an nginx
string, where a `$` or `"` would break the config. From the repo root, with
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_SESSION_STRING` in the
shell:

```bash
TELEGRAM_MCP_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic telegram-mcp-auth --namespace telegram-mcp --dry-run=client -o yaml --from-literal=TELEGRAM_API_ID="$TELEGRAM_API_ID" --from-literal=TELEGRAM_API_HASH="$TELEGRAM_API_HASH" --from-literal=TELEGRAM_SESSION_STRING="$TELEGRAM_SESSION_STRING" --from-literal=MCP_BEARER_TOKEN="$TELEGRAM_MCP_TOKEN" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > telegram-mcp/sealed-secret.yaml
```

Keep the bearer token in vaultwarden too; the MCPServer header needs it. The
pod stays in `CreateContainerConfigError` until the secret exists. If
`KUBECONFIG` is set it must be an absolute path -- see `mailserver/README.md`.

## How the session reaches the volume

The init container runs `telegram-mcp-migrate-session --target
/data/telegram_mcp_session` when `/data/telegram_mcp_session.session` is
absent, turning the sealed string into the SQLite file session the server runs
on. A string carries the auth key but not Telethon's update cursor or entity
cache, which is why upstream wants a file session for a long-running server.

The test is done in the shell, not by the migrator: it refuses to overwrite an
existing destination and exits non-zero, which would crash-loop every restart
after the first.

A consequence worth knowing: **losing the volume is survivable here.** The init
container re-derives the session from the sealed string, so there is no
re-pairing step -- unlike whatsapp-mcp, where a lost volume means scanning the
QR again. Both halves die the moment the device is revoked on the phone.

An unauthorized session is a startup failure, not a degraded server: the runner
raises `_NotAuthorized` and the pod crash-loops. Interactive login inside the
pod is not possible, by design.

## Register it in Catalyst

```bash
read -rs TELEGRAM_MCP_TOKEN
diagrid mcpserver create telegram --project test --url http://telegram.telegram-mcp.svc.cluster.local:8080/mcp --header Authorization:"Bearer $TELEGRAM_MCP_TOKEN" --description "Luis's personal Telegram, read-only (chigwell/telegram-mcp)" --wait
diagrid mcpserver tools list telegram --project test
```

Then `kubectl -n prj-14384718 get mcpservers.dapr.io -o yaml` shows what the
agent rendered, including whether the header became a `secretKeyRef`.

### What to grant

65 tools is too many to hand an agent, and naming them all would also
auto-grant anything a later tag adds. Grant the chat-reading subset and widen
deliberately:

```bash
diagrid mcpserver access grant telegram --project test --caller agent1 --allow-tools get_me,list_chats,get_chat,get_history,get_messages,list_messages,search_messages,search_global,get_message_context,list_contacts,search_contacts,get_direct_chat_by_contact,get_last_interaction
```

Three groups to leave out unless something needs them:

| Not granted | Why |
| --- | --- |
| `wait_for_events`, `wait_for_new_message`, `wait_for_settled_message`, `get_messages_after` | long-poll, up to `TELEGRAM_TOOL_TIMEOUT_SECONDS` (55s) per call; they hold an SSE stream open and will tie up the agent |
| `export_contacts`, `get_participants`, `get_recent_actions`, `get_blocked_users`, `get_privacy_settings`, `get_user_photos` | bulk personal data about other people, and group admin logs |
| `open_photo`, `get_photo_sheet`, `inspect_document`, `get_media_info` | file tools; inert anyway while `TELEGRAM_ALLOWED_ROOTS` is unset |

`transcribe_voice` is in the read-only set and so is registered, but
`TELEGRAM_TRANSCRIBE=off` leaves it with no engine -- it will refuse rather
than work. That is deliberate: the default engine wants a `GROQ_API_KEY` and
`auto` would ship voice notes from private chats to a third party.

Tool names must be spelled out explicitly for the agent too, the same way
`whatsapp_send` had to be -- see the catalyst-homelab-region notes.

## Risks this does not solve

- Anyone who can message the account can put text in front of the agent.
  Read-only means an injected instruction cannot send anything *from this
  server*, but the agent has other tools: whatsapp-mcp can send. Treat anything
  read here as untrusted input to the whole agent, not just to this server.
- The volume is in Longhorn's `default` group, so the nightly 02:00 UTC S3
  backup carries the session file. Anyone with that bucket can restore a
  working logged-in device until it is revoked on the phone. The same is true
  of the sealed string in this repo and in etcd.
- `read-only` is the project's own `readOnlyHint` annotation, not an audited
  boundary. It is a strong default, not a guarantee; re-check it on a bump.
- The device appears under **Settings → Devices** as
  `homelab-catalyst-mcp`. That is the kill switch: revoking it there works even
  if this cluster is gone.

## Bumping

The tag appears twice -- the `mcp` container's `uvx` args and the init
container's inline script -- and both carry `--exclude-newer`, which freezes
the dependency resolution around the pinned tarball (it resolved mcp 1.30.0,
telethon 1.45.0, pillow 13.0.0 and pypdf 6.21.4 on 2026-11-05). Move the date
together with the tag, in both places.

Then re-check the surface, because the grant is pinned to tool names and the
`read-only` split is upstream's annotation:

```bash
kubectl -n telegram-mcp rollout restart statefulset/telegram
diagrid mcpserver tools list telegram --project test
```

Diff that against the grant. A changed tag rolls the pod by itself; a changed
`--exclude-newer` date alone also changes the spec, so it rolls too.
