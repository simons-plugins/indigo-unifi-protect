# CLAUDE.md — UniFi Protect

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: UniFi Protect
- **Type**: Indigo plugin
- **Shortcut**: `unifi-protect` / `protect`
- **GitHub**: https://github.com/simons-plugins/indigo-unifi-protect
- **Language**: Python 3.10+, stdlib only (no `requests`, no `websockets`, no `aiohttp` — nothing in `Contents/Packages/`)

## Role in the workspace

Bridges UniFi Protect camera motion/person/vehicle/animal detection into Indigo
sensor devices, using the official UniFi OS integration API
(`https://<host>/proxy/protect/integration/v1`) — API-key auth, no login/cookies.
Also bridges Protect sensors, floodlights, doorbell chimes, NVR arm state,
and viewers (ViewPorts). Sensors, lights, chimes, and viewers are
**spec-derived, unverified against real hardware** (see
[`docs/CONTRACT.md`](./docs/CONTRACT.md)) — the reference rig owns none of
that hardware. The NVR's arm-state fields and camera support above are
both **live-verified**.

**Key architectural fact: the event WebSocket (`/subscribe/events`) is the
ONLY motion source. There is no polling fallback.** `GET /events` is a 404 —
it does not exist — and the `GET /cameras` camera object carries no motion
field (no `isMotionDetected`, no `lastMotion`). If the socket is down, motion
is unknowable for every camera; the plugin says so in device state
(`connected = false`) rather than reporting "no motion".

**Indigo booleans cannot express "unknown".** When the socket is down, the
plugin forces both `motionDetected` and `connected` to False — any trigger
that acts on motion **must gate on `connected` first**, or a dead socket
reads as an empty house. See [`docs/CONTRACT.md`](./docs/CONTRACT.md) for the
full verified API contract, the WS frame shape, and the two
duplicate/stale-keepalive traps `event_tracker.py` must handle.

**A second WebSocket, `/subscribe/devices`, pushes camera/sensor/light/chime/
NVR config+state changes live** (issue #18) — freshness-only, and
independent of the above: it has its own retry/backoff, and `connected`
still means the **events** socket alone. Losing the device socket never
touches motion, audio, or `connected`; it only means hardware/config states
fall back to the existing 60s poll cadence until it reconnects.

## Related projects

Standalone — no sibling dependencies in this workspace.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points for this project:

- **Version bump per PR**: `Info.plist` `PluginVersion`
- **Testing**: pytest + `pyproject.toml` (pylint with custom Indigo rules, 120-char lines) — mirrors `netro/tests/` (`unittest.mock`, `indigo` stubbed in `conftest.py`)
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.
- **Any PR touching `pages/*.html` must say so in the PR title.** This repo's
  releases use `generate_release_notes: true`, which builds the changelog
  entirely from PR titles. Since 2026.10.0 (issue #27) the plugin
  auto-installs `pages/cameras.html` into `Web Assets/static/pages/` on
  every startup, but that install compares content, not the PR title — a
  reader still relies on release notes to know a page changed at all,
  especially anyone who has unticked "Manage the Cameras web page" and
  copies it by hand.
- **`pages/cameras.html` is physically duplicated, not symlinked, into the
  plugin bundle** at
  `UniFi Protect.indigoPlugin/Contents/Resources/pages/cameras.html` — a
  symlink is not safe here because zip/release bundling and Indigo may not
  preserve it. `pages/cameras.html` remains the single source of truth;
  after editing it, sync the bundled copy:
  ```bash
  cp pages/cameras.html "UniFi Protect.indigoPlugin/Contents/Resources/pages/cameras.html"
  ```
  `tests/test_page_bundle.py` fails the build if the two ever drift apart.

---

**Full internal module contract** (indicative signatures for `protect_api.py`,
`protect_ws.py`, `event_tracker.py`, `plugin.py`, state table, degradation-path
test list): see [`docs/CONTRACT.md`](./docs/CONTRACT.md).
