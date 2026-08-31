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

### Camera hardware and config states

Each device also reports the camera's current hardware/config as read-only
states: `cameraModel`, `videoMode`, `hdrType`, `micEnabled`, `micVolume`,
`ledEnabled`, `osdNameEnabled`, and `osdDateEnabled`. Unlike the motion/audio
states above, these come from the cached `GET /cameras` object rather than
the event socket, and they are only written when that object is available —
a failed lookup leaves them at their last-known value rather than overwriting
real hardware state with a fabricated `False`/`""`. `dev.model` is also kept
in sync with the camera's hardware type, so the device list shows e.g. "UVC
G5 Turret Ultra" instead of the generic "Protect Camera".

Since v2026.8.0 (issue #18), these hardware/config states also update
**live**: the plugin keeps a second, independent WebSocket
(`/subscribe/devices`) open alongside the events socket above, and applies a
camera/sensor/light/chime/NVR config change (e.g. flipping the status LED
from the UniFi app) the moment the controller pushes it, rather than waiting
for the next 60s poll. `connected` still means the **events** socket only —
losing the device socket only makes hardware/config states as stale as they
were before this feature existed (the 60s poll cadence); it never touches
motion, audio, or `connected`.

Deliberately **not** available, because it needs the private (not the
official integration) API: firmware version, camera IP, `isDark`, IR LED
mode/level, zoom position, motion tuning, and wifi stats. See
[`docs/API-REFERENCE.md`](./docs/API-REFERENCE.md) (added by PR #10) for
the full verified field list.

### Camera control actions

This is the plugin's first **write** path — everything above only reads.
Five device actions, all under **Device Actions**, send a partial
`PATCH /cameras/{id}`; the console applies just the fields sent and the
plugin refreshes the camera-info states above from the response
immediately, rather than waiting for the next refresh:

- **Set Status LED** — on / off / toggle. Toggle inverts the camera's own
  current `ledEnabled` state, so it does the right thing even if the LED
  was last changed from the UniFi app, not this plugin.
- **Set OSD Overlay** — show/hide the camera name, date, and logo, and set
  the overlay location (top/bottom, left/middle/right). Each field
  defaults to **Unchanged**; only the fields you actually change are sent,
  and leaving everything unchanged is rejected — there would be nothing to
  do.
- **Set Video Mode** — the picker is populated live from the camera's own
  `featureFlags.videoModes`, so it only ever offers modes that camera
  actually supports. If the camera isn't cached yet, it falls back to
  Protect's published mode list with each option marked *(unverified)* —
  picking one there is still checked against the real camera before
  anything is sent.
- **Set HDR Mode** — auto / on / off.
- **Set Microphone Volume** — 0–100.

Each action checks the camera's `featureFlags` **before** sending
anything — `setStatusLed` needs `hasLedStatus`, `setHdrMode` needs
`hasHdr`, `setMicVolume` needs `hasMic` — because not every camera has
every capability (one on the reference rig reports no speaker at all). A
camera that doesn't support the action gets an Event Log error instead of
a silently-ignored PATCH.

**A refused write always shows up in the Event Log**, including the exact
field the controller rejected (e.g. `/videoMode: must be equal to one of
the allowed values`) when Protect's API returns one. A successful write
never gets silently swallowed either way — the camera-info states above
are only ever updated from an actual 2xx response, never assumed. A
network problem or a controller error reports its outcome as **unknown**,
not as a refusal — the write may well have landed even though this
particular response couldn't confirm it — and the plugin re-reads the
camera to catch up rather than guessing either way.

Verified live against a UNVR on Protect **7.2.105**, 2026-08-31: the API
key alone authorises these writes, the same as every read in this plugin —
no separate write credential was needed.

## Live stream URLs

Each camera device can optionally expose its RTSPS live-stream URLs as
device states, for a control page or an external viewer (VLC, ffplay, a
home-theatre system) to pull actual video rather than the periodic stills
above.

**This is off by default, and it should stay off unless you have a real use
for it.** The URL is not just a network address — it embeds an access
token, so anyone who can read it can stream from the camera. Indigo device
states are readable by anything that can read the Indigo database (other
plugins, control pages, scripts), so turning this on widens who can watch
your camera beyond "people with the UniFi app." Tick **Expose RTSPS stream
URLs** on the device only when something concrete needs it; unticking it
immediately clears the stored URLs back to empty, it doesn't just stop
refreshing them.

When enabled, four states are populated: `streamUrlHigh`, `streamUrlMedium`,
`streamUrlLow`, and `streamUrlPackage`. `streamUrlPackage` stays empty
unless the camera reports `hasPackageCamera` — most cameras don't have a
package-detection lens and the controller returns `null` for it.

They refresh automatically after the device starts and on every event-socket
reconnect — but not instantly: fetching a stream URL talks to the
controller, and that happens in the background, throttled alongside every
other request this plugin makes, so **the first URLs typically appear a
few seconds after the plugin connects**, not the moment you tick the
checkbox or the socket comes up. With several opted-in cameras, they fill
in one at a time rather than all at once. If a viewer stops working, fire
the plugin's **Refresh Stream URLs** action (or Send Status Request, which
is synchronous and reports the outcome immediately) rather than assuming
the URL is permanently dead: on the one console this was verified against,
the token was identical across two calls ten seconds apart, but whether it
rotates over a longer window (hours/days) is untested, and a stale token
would look exactly like a broken stream. A momentary failure (e.g. a
rate limit) never blanks a working URL — the plugin keeps whichever value
it already had rather than clearing a stream a viewer might be using.

A one-line example: copy the `streamUrlHigh` state's value and hand it
straight to a player -- `ffplay "rtsps://192.168.0.10:7441/<token>?enableSrtp"`,
or paste the same URL into VLC's **Open Network Stream** dialog.

## Other Protect devices -- sensors, floodlights, chimes, NVR

> **Built from the official OpenAPI spec, never exercised on real
> hardware.** Every camera feature in this plugin (above) was verified
> against a live UniFi Protect console. Sensors, floodlights, and chimes
> were not: the reference console has none, and `GET /sensors`, `GET
> /lights`, and `GET /chimes` all return `[]` there. This code is built
> entirely from Protect's published OpenAPI 3.1 spec (v6.2.83) --
> field names, shapes, and event types should be right, but nothing here
> has been proven against a real Protect sensor, floodlight, or chime.
> **If something looks wrong, please open an issue with a debug capture**:
> run `docs/ws_probe.py` while triggering the sensor/light/chime, and
> attach its output alongside a `GET /sensors` (or `/lights`, `/chimes`)
> JSON dump from your console. The NVR device below is the one exception
> -- its arm-state fields were captured live.
>
> **One known limitation, independent of the spec-derived caveat above:**
> a leak that began before the plugin started (or before the event socket
> last reconnected) is not shown until the sensor reports again -- the
> `leakDetected`/`alarmTriggered`/`tampered` booleans only ever reflect a
> LIVE event, because Protect's own poll response has no "currently
> active" flag for these three, only a timestamp of when it was last
> reported. The timestamps themselves (`lastLeak`/`lastAlarm`/
> `lastTamper` on Protect Sensor) are still shown, so the information
> isn't lost -- just not folded into the boolean.

Each class gets its own Indigo device type, added and configured the same
way as a camera: create a device, pick the Protect object from the
dropdown (populated live), done. Use **Discover Devices** (plugin menu,
renamed from "Discover Cameras") to list everything your console reports
across all four classes.

### Polling

Sensors, floodlights, and chimes have no live push feed from Protect for
most of their state -- only cameras get one. This plugin polls each class
over REST every **60 seconds**, and only for classes that actually have a
registered Indigo device (a console with sensors but no chimes never
triggers a chime poll). Sensors and floodlights *also* receive live
updates over the same camera event socket for the fields that have a
matching Protect event (motion/leak/alarm/tamper lifecycle events, and the
open/closed/battery-low/extreme-value/PIR-motion "pulse" notifications) --
the poll is what keeps everything else (temperature, battery percentage,
light mode, chime ring settings, ...) current, and it's also what
corrects a live motion flag that's gotten stuck (if Protect's own poll
answer says motion has stopped, that wins).

A poll failure logs one ERROR per class per outage (not once a minute, and
one INFO when it recovers), and PROACTIVELY marks that class's own state
string (`sensorState`/`lightState`/`chimeState`/`armStatus`) `"unavailable"`
-- every other value for that class (temperature, isOpen, ring volume, ...)
is left exactly where it last was, not re-read from a stale cache and not
blanked either. The same happens if a device that's still configured in
Indigo simply stops appearing in Protect's list (removed, or the console
briefly returned an empty list) -- a WARNING names the device, and its
values are held the same way. Before the very first poll ever completes
(a brand-new device, or one added while the controller was down), the
plugin writes only what it can genuinely vouch for -- lifecycle-driven
booleans and "unavailable" -- and never fabricates a fresh-looking
False/0/empty value for anything poll-derived. `connected` always reflects
the event socket itself, independent of whether polling is succeeding --
a device won't get stuck reporting disconnected just because its first
poll after a reconnect happened to fail.

### Protect Sensor

Tracks a Protect multi-purpose sensor (door/window/garage contact, leak,
motion, tamper, and a smoke/CO/glass-break alarm listener, plus
temperature/humidity/light-level readings on models that have them).

States: `isOpen`, `motionDetected`, `leakDetected`, `alarmTriggered`,
`alarmType`, `tampered`, `batteryLow`, `temperature`, `humidity`,
`lightLevel`, `mountType`, `sensorState`, `connected`, `lastMotion`,
`lastOpenChange`, `lastLeak`, `lastAlarm`, `lastTamper`, `lastPoll`.

**Which state drives the device's on/off status** (what a "device turned
on" trigger watches) is the **Primary state** device setting: `Auto`
(default) picks by mount type -- door/window/garage mounts use `isOpen`,
a leak mount uses `leakDetected`, no mount uses `motionDetected` -- or
pin it explicitly to Open/Closed, Motion, Leak, or Alarm.

**Battery percentage is Indigo's native battery state**, not a plugin
state -- it shows up wherever Indigo already shows battery level for any
device, via the `SupportsBatteryLevel` device property. (The controller
API marks the field this reads, `batteryStatus`, "deprecated" in favour of
one that doesn't otherwise exist in the spec -- see `docs/CONTRACT.md`.)

`motionDetected`/`leakDetected`/`alarmTriggered`/`tampered` are
live-event-driven, so they follow the same honesty rule as camera motion:
when the event socket is down they go `False`, not "unknown". Everything
else here (`isOpen`, `batteryLow`, `temperature`, ...) comes from the
60-second poll and is kept at its last-known value through a socket
outage, the same way a camera's hardware states survive one.

**`leakDetected`/`alarmTriggered`/`tampered` cannot recover a pre-existing
condition across a restart** (see the banner above): Protect's poll only
gives a timestamp of the last report for these three, never a "still
active" flag, so the boolean starts `False` until a new live event
arrives. `lastLeak`, `lastAlarm`, and `lastTamper` hold the poll's own
timestamp for each (blank if the sensor has never reported one), so the
fact isn't lost even while the boolean can't show it. `isOpen` doesn't
have this gap -- Protect's poll gives a real current open/closed flag.

### Protect Light (floodlight)

States: `isDark`, `pirMotionDetected`, `forceEnabled`, `ledLevel`,
`lightMode`, `lightState`, `connected`, `lastMotion`, `lastPoll`. The
device's on/off state is the light's actual LED (`isLightOn`).

**Turning the device "on" or "off" force-enables/disables the light's main
LED** (`PATCH .../lights/{id}` with `isLightForceEnabled`). **"Off" only
clears the force flag -- it does not disable the floodlight's own motion
mode.** If the light's mode is set to turn on for motion, it can still
light up on its own right after you turn it "off" here. There's a
**Set LED Level** action (1-6) for the light's brightness.

### Protect Chime

States: `chimeState`, `pairedCameraCount`, `ringVolume` (the first paired
camera's ring volume -- skipped entirely if the chime has no ringtone
settings), `connected`, `lastPoll`. There's a **Set Chime Volume** action
(0-100) that applies the same volume to every camera paired to the chime;
it errors without doing anything if the chime has no ring settings to set
volume on in the first place (nothing paired to it yet).

### Protect NVR

One device, no picker -- there is only one NVR per console. Tracks your
UniFi OS console's arm/disarm state: `nvrName`, `nvrModel`,
`protectVersion`, `armStatus`, `armedAt`, `breachDetectedAt`,
`breachEventCount`, `connected`, `lastPoll`. `armStatus` reads
`"unavailable"` before the first successful poll, or if the console never
reports an arm state at all. Arm/disarm itself is not exposed as an
action -- it isn't part of the published integration API.

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
