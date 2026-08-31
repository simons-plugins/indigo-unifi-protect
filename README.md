# UniFi Protect for Indigo

Bridges UniFi Protect camera motion (person, vehicle, animal) and audio
(speech, baby cry, smoke/CO alarm sounds) detection into
[Indigo](https://www.indigodomo.com) sensor devices, via the official
UniFi OS integration API.

## Requirements

- Verified against UniFi Protect **7.2.105**. Requires the integration API
  (`/proxy/protect/integration/v1`) and console API keys; older releases may
  not work.
- A UniFi OS console (UDM/UNVR/Dream Machine) reachable on your network
- An API key for that console

## Getting an API key

On your UniFi OS console: **Settings → Control Plane → Integrations →
Create API Key**.

## Setup

1. Install the plugin.
2. In the plugin config, enter the console's host/IP and the API key.
   Leave **Verify SSL certificate** unchecked unless you've installed a
   trusted certificate on the console — UniFi OS consoles ship a self-signed
   cert by default.
3. Create a device, and pick a camera from the **Camera** dropdown (populated
   live from the console).
4. Use **Discover Cameras** (plugin menu) or **Refresh Cameras** (plugin
   action) to re-poll the camera list at any time.

## Camera snapshot page

The plugin ships an optional HTML page — [`pages/cameras.html`](./pages/cameras.html) —
that turns the camera devices into something you can actually look at:

- **A dropdown over every Protect camera** in Indigo, discovered live by plugin
  id. Add a camera device and it appears here; no editing the page.
- **Take snapshot** — fires this plugin's `takeSnapshot` action for the selected
  camera and reloads the image. No per-camera action group needed.
- **Auto-refresh** every 30s (off by default) for a rough live view.
- **Live motion state** — motion, person, vehicle, animal, camera state and
  event-socket health, polled every 5s.

It also does two things deliberately:

- **It tells you how old the picture is**, read from the file's `Last-Modified`
  header, and turns amber past two minutes. A camera page showing a silently
  stale still is worse than showing nothing.
- **It warns when the event socket is down**, in words, and tells you not to
  trust the motion indicators until it clears — see
  [Reading motion state correctly](#reading-motion-state-correctly) below.

### Install

Copy the file into Indigo's **user pages** folder:

```bash
cp pages/cameras.html \
  "/Library/Application Support/Perceptive Automation/Indigo 2025.2/Web Assets/static/pages/"
```

That folder survives plugin upgrades, and it is one of the two directories the
[Domio](https://domio-smart-home.app) iOS app scans. Restart the Domio plugin
and the page appears as **Cameras**.

> It is deliberately **not** bundled inside this plugin. Domio only scans its
> own plugin folder and `Web Assets/static/pages` — a page inside *this*
> plugin's `Contents/Resources` can never be discovered by it, so shipping a
> copy there would only create two files that drift apart.
> ([simons-plugins/indigo-domio-plugin#24](https://github.com/simons-plugins/indigo-domio-plugin/issues/24)
> tracks fixing that upstream.)

### Use it in a browser

```
https://<indigo-host>:8176/static/pages/cameras.html?api-key=<your-key>
```

The `?api-key=` form is how the page authenticates outside the Domio app.

> **Why the page fetches images rather than using `<img src>`:** Indigo's web
> server requires authentication for everything under `Web Assets`, including
> the snapshot JPEGs. An `<img src="...">` tag cannot carry an `Authorization`
> header, so it gets a silent 401 that looks exactly like a missing file. The
> page fetches the bytes with the bearer token and hands the `<img>` a blob URL
> instead.

### Where snapshots are stored

```
/Library/Application Support/Perceptive Automation/Indigo <version>/Web Assets/images/unifi-protect/camera_<deviceId>.jpg
```

Served by Indigo at `/images/unifi-protect/camera_<deviceId>.jpg`. Files are
keyed by **Indigo device id**, not camera name, so renaming a device doesn't
orphan its image. They live outside the plugin bundle on purpose — Indigo
replaces `Contents/` on every plugin upgrade, which would delete them.

More detail in [`pages/README.md`](./pages/README.md).

## Reading motion state correctly

**Indigo booleans cannot express "unknown".** When the Protect event
WebSocket is down, this plugin forces both `motionDetected` and `connected`
to False — there is no third state for "I can't tell". Any trigger that
acts on motion **must gate on `connected` first**, or a dead socket reads
as an empty, quiet house. This is the single most important thing to know
before wiring this plugin into an automation.

### Audio detection

Cameras with a microphone also report **audio events** — speech, a baby
crying, or a smoke/CO alarm sounding — over the same event socket as
motion, but tracked completely separately:

- `audioDetected` is true while any audio event is active on the camera
  (even before Protect has classified *what* it heard — classification
  lands about a second after the `add` frame arrives).
- `speechDetected`, `babyCryDetected`, `smokeAlarmDetected`, and
  `coAlarmDetected` are true when that specific type is part of the
  currently active audio event(s).
- `lastAudio` / `lastAudioTypes` record the most recent audio event, the
  same way `lastMotion` / `lastDetectTypes` do for motion.

**By default, `onOffState` — what a "device turned on" trigger watches —
goes on for motion, speech, or a baby crying.** Uncheck **Audio counts as
activity** on the device to exclude speech/baby-cry and have `onOffState`
track motion only. **A smoke or CO alarm sound never turns the device on**,
checkbox or not — build automations against `smokeAlarmDetected` /
`coAlarmDetected` directly, not against `onOffState`, so a real alarm is
never mistaken for (or buried under) routine motion/presence handling.

The same `connected` gating above applies here too: when the event socket
is down, the live audio booleans and `lastAudioTypes` go False/empty
exactly like `motionDetected` does (`lastAudio` is a historical
timestamp and is kept, same as `lastMotion`). Silence is not safety —
it can just as easily mean the plugin can't hear anything right now.

## Latency

Motion detection rides the Protect event WebSocket, not polling. Measured
from Protect's own event timestamp to frame receipt (`docs/ws_probe.py`, 2
events, ±1s — the capture's receive timestamps only have whole-second
resolution):

- **Motion on**: ~1–3s
- **Motion off**: ~6–7s
- **Audio** (one live sample, 2026-08-26): the `add` trailed Protect's
  own `start` by ~16s and the `end` arrived ~12s after it — do not
  assume motion-like latency for audio.

These figures exclude the plugin's own message parsing and the Indigo
state-write. This is fine for occupancy and security automation, but the
off-latency in particular means this plugin is **not a PIR replacement for
lighting triggers** — don't wire it directly to a "turn lights off when
motion stops" rule expecting sub-second response.

## Further reading

[`docs/API-REFERENCE.md`](./docs/API-REFERENCE.md) — the full official API
surface: every endpoint, every `/subscribe/events` event type, what the
official API can't give you that the private one can, and why. Internal
module contract is in [`docs/CONTRACT.md`](./docs/CONTRACT.md).
