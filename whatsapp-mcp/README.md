# whatsapp-mcp

[go-whatsapp-web-multidevice](https://github.com/aldinokemal/go-whatsapp-web-multidevice),
linked to Luis's personal WhatsApp as a device, serving MCP (streamable HTTP)
at `/mcp` for the Catalyst agents. The REST API, the dashboard and `/mcp` share
port 3000 and the same basic auth.

Catalyst never runs this. The MCPServer registered in project `test` is only a
URL; the sidecar Catalyst runs for it in `prj-14384718` calls
`http://whatsapp.whatsapp-mcp.svc.cluster.local:3000/mcp` and enforces the
per-caller tool grants. The registration and grants live in Catalyst, not
here -- the agent writes the dapr `MCPServer` object into `prj-14384718`
itself, the same way it writes every Component there.

## Before the first sync

The Application and the AppProject destination live in `ansible-local-network`
(`manifests/whatsapp-mcp-app.yaml`, the `whatsapp-mcp` destination in
`manifests/project.yaml`, a task in `argocd.yaml`). Push this side first, with
the sealed secret, then apply that side from `ansible-local-network`:

```bash
kubectl apply -f playbooks/kubernetes/cluster/argocd/manifests/project.yaml -f playbooks/kubernetes/cluster/argocd/manifests/whatsapp-mcp-app.yaml
```

## Create the sealed secret

`APP_BASIC_AUTH` holds two accounts: `me` for the dashboard, `catalyst` for
the MCPServer header. **Either one has full control of the account** -- the
server has no read-only users. Hex passwords on purpose: `,` and `:` are the
separators in that variable. From the repo root:

```bash
ME_PW=$(openssl rand -hex 24) CATALYST_PW=$(openssl rand -hex 24)
kubectl create secret generic whatsapp-auth --namespace whatsapp-mcp --dry-run=client -o yaml --from-literal=APP_BASIC_AUTH="me:$ME_PW,catalyst:$CATALYST_PW" | kubeseal --controller-namespace kube-system --controller-name sealed-secrets-controller --format yaml > whatsapp-mcp/sealed-secret.yaml
```

Put both passwords in vaultwarden before closing the shell. The pod stays in
`CreateContainerConfigError` until the secret exists. If `KUBECONFIG` is set
it must be an absolute path -- see `mailserver/README.md`.

## Pair the phone

The dashboard is not exposed anywhere; reach it through the API server:

```bash
kubectl -n whatsapp-mcp port-forward svc/whatsapp 3000:3000
```

Open <http://localhost:3000>, sign in as `me`, start a login and scan the QR
code from the phone under **Settings → Linked devices → Link a device**. The
session lands on the volume; a restart does not need a re-scan. To revoke it,
remove the device on the phone -- that works even if this cluster is gone.

## Register it in Catalyst

```bash
read -rs CATALYST_PW   # the `catalyst` password from vaultwarden
diagrid mcpserver create whatsapp --project test --url http://whatsapp.whatsapp-mcp.svc.cluster.local:3000/mcp --header Authorization:"Basic $(printf 'catalyst:%s' "$CATALYST_PW" | base64)" --description "Luis's personal WhatsApp (go-whatsapp-web-multidevice)" --wait
diagrid mcpserver tools list whatsapp --project test
diagrid mcpserver access grant whatsapp --project test --caller whatsapp-agent --allow-tools whatsapp_chat
```

Then `kubectl -n prj-14384718 get mcpservers.dapr.io -o yaml` shows what the
agent rendered, including whether the header became a `secretKeyRef`.

`whatsapp-agent` is the only caller. Every other agent that needs WhatsApp
asks it, as a teammate, rather than holding a grant here. It is defined, and
its other grants are listed, in `whatsapp-bridge/README.md`.

### What to grant

Each of the seven tools takes an `action` argument, and grants match tool
names only, so a grant is all of a tool's actions or none of them:

| Tool | Grant? | Why |
| --- | --- | --- |
| `whatsapp_chat` | yes | `list_chats`, `list_contacts`, `get_messages` -- and `archive` |
| `whatsapp_send` | no | it sends as Luis, to anyone, and also takes `scheduled_at` / `recurrence`, so it can set up messages that keep firing. `whatsapp-agent` sends from its own number instead (`whatsapp-bridge/`) |
| `whatsapp_schedule` | no | manages those scheduled sends |
| `whatsapp_message` | no | `delete`, `revoke`, `edit`, plus `download_media` |
| `whatsapp_app` | never | `logout` unlinks the device |
| `whatsapp_group` | never | create, leave, add/remove participants |
| `whatsapp_newsletter` | no | follows/unfollows channels |

There is no message-search tool.

## Risks this does not solve

- It is an unofficial client. WhatsApp can ban the number.
- Anyone who can message the number can put text in front of the agent. With
  `whatsapp_send` granted, an injected instruction can send what the agent
  has read to anyone. Keep read-only agents read-only.
- The volume is in Longhorn's `default` group, so the nightly 02:00 UTC S3
  backup carries the session keys and the chat database with it. Anyone with
  that bucket can restore a working linked device until it is removed on the
  phone.

## Bumping

The image is pinned by digest, and the dashboard by sha256
(`APP_UI_ASSET_SHA256`): the server fetches gowa-ui's latest GitHub release
every 3h and refuses any other build. A new gowa-ui release is ignored and the
cached v1.8.0 keeps being served -- but a fresh volume would get the fallback
page until the pin moves. Take the new hash from the release's
`gowa-ui.html.sha256` asset.
