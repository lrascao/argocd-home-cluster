# eufy-mqtt

Camera events from the two eufy cameras onto MQTT: `ha-eufy-sdk-bridge` holds
the eufy cloud session and speaks WebSocket/HTTP, and a small shim republishes
what it broadcasts onto mosquitto.

Going through MQTT rather than through Home Assistant is the point — mosquitto
becomes the source of truth and HA, node-red and anything later are all just
subscribers.

## Before the first sync

The Application and the AppProject destination are not in this repo -- they
live in `ansible-local-network` under `playbooks/kubernetes/cluster/argocd/`
(`manifests/eufy-mqtt-app.yaml`, a destination entry in `manifests/project.yaml`,
and a task in `argocd.yaml`). Argocd rejects the application outright until the
project lists the namespace, so both halves have to land before this syncs.

Two more things, both of which will otherwise fail visibly rather than
silently:

1. **Replace `charts/local/templates/sealed-secret.yaml`.** It ships as a
   placeholder that cannot decrypt — the command is in that file's header. Use
   a dedicated eufy account invited to the real one; the account allows one
   active session, so sharing the app's credentials means the two fight.
2. **Build and push the shim image**, from `ansible-local-network`:
   ```
   make -C services/eufy-mqtt push
   ```
   The tag in `values.yaml` and `services/eufy-mqtt/VERSION` are two halves of
   one thing and nothing checks they agree.

Then complete 2FA once, against the running bridge — see the shim's README in
`ansible-local-network/services/eufy-mqtt/`. The session persists on the PVC,
so it is a first-run step and a recovery step, not a routine one.

## Topics

| topic | retained | |
| --- | --- | --- |
| `eufy/bridge/status` | ✅ | `online` / `offline` (offline is an MQTT last-will) |
| `eufy/bridge/health` | ✅ | `{status, reason, ts}` |
| `eufy/bridge/auth` | ✅ | the bridge's auth state |
| `eufy/bridge/devices` | ✅ | device count |
| `eufy/bridge/heartbeat` | ✅ | `{ts, last_event_at, devices, auth, ws}` |
| `eufy/<sn>/info` | ✅ | name, model, capabilities |
| `eufy/<sn>/<event>` | — | `personDetected`, `vehicleDetected`, `motion`, … |
| `eufy/<sn>/event_image` | ✅ | JPEG: the frame the detection fired on |

## Alert on health, never on silence

The eufy cloud auth breaks recurrently and silently: login still reports `ok`,
the cloud starts returning an empty device list, and no events arrive again
(mega-yfue/eufy-sdk#182 — twice in two weeks in September 2026, no automatic
recovery). Nothing downstream can see that, because an absence of camera events
is indistinguishable from a quiet house.

So alert on `eufy/bridge/status` going `offline`, on `eufy/bridge/health.status`
going `degraded` (which encodes that bug's signature — auth ok *and* zero
devices), and on a `eufy/bridge/heartbeat` older than ~3 minutes. Do not try to
infer health from the event topics.

## Why there is no video here — and why that is a choice, not a limit

Events are what this app is for. Video is available and deliberately unused.

It is worth being precise about that, because the obvious research says
otherwise: eufy's own support article states the Floodlight Cam "does not
support NAS/RTSP because of the hardware restriction", and the S100 has no such
setting. Both are true of those cameras **standalone**. Attached to the
HomeBase 3 here, RTSP is published by the HomeBase instead, and both cameras
expose a writable `rtspStream` property (`1145 NAS_STREAM_SWITHC`) plus a
read-only `rtspUrl`:

```
device.properties T8425… →  rtspStream  bool, writable
                            rtspUrl     string, readonly
```

Both are `false` today. Turning one on is `device.set` over the bridge's
WebSocket, or the toggle in the app, and it is reversible.

So the door to go2rtc, Frigate and continuous recording is open — the bridge
bundles go2rtc and the Service already exposes 8554. It stays shut because
notifications do not need it, and continuous video would want storage that
longhorn replication is the wrong home for.
