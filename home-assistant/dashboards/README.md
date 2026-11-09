# Dashboards

Everything that appears as a dashboard in this Home Assistant's sidebar. Two of
these files are live configuration; two are records of things that cannot be
driven from git. Knowing which is which is the whole point of this file.

| File | Status | How it reaches Home Assistant |
|---|---|---|
| `climate.yaml` | **source of truth** | ConfigMap → `/config/dashboards`, `mode: yaml` |
| `tablet.yaml` | **source of truth** | ConfigMap → `/config/dashboards`, `mode: yaml` |
| `map.yaml` | record only | still `mode: storage` on the PVC |
| `energy-preferences.yaml` | record only | built-in panel, no YAML schema exists |

## The two that are live

Edit `climate.yaml` or `tablet.yaml`, push, and the change lands on its own.
No pod restart and no reload action, which is worth understanding rather than
trusting:

`../templates/dashboards-configmap.yaml` globs this directory into a ConfigMap,
and `additionalMounts` in `../values.yaml` mounts it at `/config/dashboards` as
a directory. When the ConfigMap changes, kubelet writes a new timestamped
directory and swaps the `..data` symlink, so the projected file's mtime moves.
`LovelaceYAML._load_config` stats the file on each load and re-reads it whenever
its cache is older, then fires `EVENT_LOVELACE_UPDATED` — so an already-open
browser updates too. Allow about a minute for the kubelet sync.

The cost: **the UI editor is read-only for these two.** Every change is a
commit. That is inherent to `mode: yaml`, not a limitation of this setup.

Registration — title, icon, sidebar visibility — is declared in the `lovelace:`
block in `../values.yaml` rather than in `.storage/lovelace_dashboards`. Once
the switchover at the bottom of this file is done, change it there and not in
Settings → Dashboards.

## The two that are not

`map.yaml` cannot be converted. `_validate_url_slug` requires a dashboard's
url_path to contain a hyphen and exempts only `lovelace`; `map` has none, and
declaring it fails validation for the entire lovelace integration, not just that
one dashboard. Renaming it would fix that at the cost of the `/map` URL. Since
the file is a four-line `strategy: map` stub with nothing hand-built in it, it
stays in storage mode and this copy is only a record.

`energy-preferences.yaml` is not a dashboard at all. `/energy` is a built-in
panel with no Lovelace config; what it has is `.storage/energy`, the mapping of
sensors to grid, solar, battery and per-device consumption. There is no YAML
schema for energy sources, so this can never be applied — it is something to
retype under Settings → Energy if the PVC is lost. Two things it records:

* **No solar source.** Only `battery` (the Venus DC charge/discharge pair) and
  `grid` (`ac_in_grid_consumption_on_l1`, `ac_in_feed_in`). PV production does
  not reach the Energy dashboard, so its distribution view cannot attribute
  anything to the panels.
* **No tariffs.** Every cost and price field is null with
  `cost_adjustment_day: 0.0`, so the panel is kWh-only.

## Climate and Tablet overlap

These two were card-for-card identical when they were exported, differing only
in the first view's label (Climate calls it `Overview` at path `home`, Tablet
calls it `Climate` at path `climate`) and in the `energy` view that only Tablet
has. They have since diverged: Climate's Overview grew a third section with the
Sonoff room graphs, and Tablet did not.

That is the drift to expect — nothing dedupes them, so a change to one is never
a change to the other. The difference is at least legible in a diff now that
both are in git, which is more than storage mode offered.

Tablet's `energy` view is built from core `energy-*` cards that read the
preferences above. It holds no configuration of its own.

## What the HACS resources do not carry

`tablet.yaml` opens with a `swipe_nav:` key, which belongs to
`hass-swipe-navigation`, one of two HACS frontend resources registered in
`.storage/lovelace_resources`. HACS installs its JavaScript under
`/config/www/community` on the PVC, so none of that is in this repo. Restore
`tablet.yaml` into a Home Assistant without it and the key is ignored silently:
no error, the dashboard just stops swiping.

The other resource is `kiosk-mode`, which no dashboard here configures — there
is no `kiosk_mode:` key anywhere in them — so whatever it does is driven from
outside these files. Nothing in this directory reproduces it.

Resources stay HACS-managed because `lovelace.mode` is `storage`, which is what
`resource_mode` falls back to. Home Assistant 2026.6 deprecates that fallback
and removes it in 2026.8, so this will want an explicit `resource_mode: storage`
before then.

## Re-exporting the two records

```sh
for f in lovelace.map energy; do
  kubectl exec -n home-assistant home-assistant-0 -c home-assistant -- \
    cat "/config/.storage/$f"
done
```

Each `.storage` file is JSON wrapped in a `version`/`key`/`data` envelope. For a
dashboard the payload is `.data.config`; for `energy` it is `.data`. These files
hold that payload with the envelope dropped.

Climate and Tablet no longer have a `.storage` file to export from — see below.

## One-time switchover

**Not yet done.** A url_path cannot be registered twice: if
`.storage/lovelace_dashboards` still has `dashboard-climate` or
`dashboard-tablet` when the `lovelace:` block goes live, one registration is
dropped with `Panel url path %s is not unique`.

This app syncs itself — `syncPolicy.automated` with `selfHeal` and `prune` —
so pushing is what triggers it. The deletion has to happen **before the push**,
not between the push and a manual sync:

1. Delete **Climate** and **Tablet** in Settings → Dashboards. This also
   deletes their `.storage/lovelace.*` configs — Home Assistant calls
   `async_delete()` — so `climate.yaml` and `tablet.yaml` here become the only
   copies that exist. That is the intent, and it is why they were exported
   before any of this was wired up.
2. Push. Argo syncs on its own, the init container merges the new `lovelace:`
   block into `/config/configuration.yaml`, the ConfigMap mounts, and both
   dashboards come back at the same URLs.

Between those steps the two dashboards are missing from Home Assistant. Nothing
else is affected.
