# API reference — UniFi Protect integration API

Answers "why don't we show X?" so it is never rediscovered. For the plugin's
own module contract (internal signatures, `EventTracker` semantics, state
IDs, rate limit), see [`CONTRACT.md`](./CONTRACT.md) — this file is about the
upstream API surface, not this plugin's code.

## Where the truth lives

The vendored spec, [`protect-openapi-v6.2.83.json`](./protect-openapi-v6.2.83.json),
is the official UniFi Protect Integration API OpenAPI 3.1 document, downloaded
2026-08-31 from `https://developer.ui.com/protect/v6.2.83/openapi.json`. Ubiquiti
publishes this per Protect version — swap `v6.2.83` in that URL for whatever
version you're targeting. v6.2.83 was the newest version the docs site listed
on 2026-08-31; everything else in this file was verified live against a
controller running Protect **7.2.105**, which is newer than the spec. Every
known live-vs-spec gap is listed here, not just wherever it happens to come
up below:

- `GET /nvrs` returns `armMode`, plus `type`, `guid`, `mac` — none of the
  four are in the spec's `nvr` schema (see the `GET /nvrs` section below).
- Camera objects carry `guid`, `type`, `hasPackageCamera` live (and in
  `CONTRACT.md`'s observed key list), none of which are in the spec's
  `camera` schema. The RTSPS section below relies on `hasPackageCamera` —
  flagged there too as undocumented.
- `/v1/users` and `/v1/bridges` are live REST endpoints; neither path
  exists in the spec (see the endpoint table below).
- The observed `/subscribe/devices` `bridge` update frame carried `guid`,
  which is not a property of the spec's `bridge` schema.
- The live `PATCH` 400 error body (`issues`, `isUserError`,
  `AJV_PARSE_ERROR`) is richer than the spec's generic `genericError` shape
  (`error`, `name`, optional `cause`) — see Writes below.

**The console itself serves no docs endpoint.** Probed 2026-08-31 against the
same controller — all 404:

```
/proxy/protect/integration/docs
/proxy/protect/integration/v1/docs
/proxy/protect/integration/openapi.json
/proxy/protect/integration/v1/openapi.json
/proxy/protect/integration/api-docs
/proxy/protect/integration/swagger.json
```

The spec has to come from `developer.ui.com`; there is no way to pull it from
the console.

## Endpoints the integration API has

All paths are under `/proxy/protect/integration` (spec paths below omit that
prefix, matching the spec's own `/v1/...` convention). 25 paths, from the
vendored spec's `paths` object, plus two more confirmed live but absent from
the spec (see below the table):

| Path | Methods | Used by this plugin |
|---|---|---|
| `/v1/meta/info` | GET | ✅ connectivity check |
| `/v1/cameras` | GET | ✅ camera list / discovery |
| `/v1/cameras/{id}` | GET, PATCH | ✅ GET only |
| `/v1/cameras/{id}/snapshot` | GET | ✅ |
| `/v1/cameras/{id}/rtsps-stream` | GET, POST, DELETE | ✅ GET/POST (issue #7); DELETE (issue #25) |
| `/v1/cameras/{id}/ptz/patrol/start/{slot}` | POST | ✅ (issue #19) — slot 0-4 |
| `/v1/cameras/{id}/ptz/patrol/stop` | POST | ✅ (issue #19) |
| `/v1/cameras/{id}/ptz/goto/{slot}` | POST | ✅ (issue #19) — slot 0-9, see below |
| `/v1/cameras/{id}/disable-mic-permanently` | POST | no |
| `/v1/cameras/{id}/talkback-session` | POST | no |
| `/v1/sensors` | GET | no — returns `[]` on reference rig |
| `/v1/sensors/{id}` | GET, PATCH | no |
| `/v1/lights` | GET | no — returns `[]` on reference rig |
| `/v1/lights/{id}` | GET, PATCH | no |
| `/v1/chimes` | GET | no — returns `[]` on reference rig |
| `/v1/chimes/{id}` | GET, PATCH | no |
| `/v1/viewers` | GET | ✅ implemented, UNVERIFIED (issue #22) — returns `[]` on reference rig |
| `/v1/viewers/{id}` | GET, PATCH | ✅ implemented, UNVERIFIED (issue #22) |
| `/v1/nvrs` | GET | no (issue #8) |
| `/v1/liveviews` | GET, POST | ✅ GET implemented, UNVERIFIED (issue #23); POST unbuilt — read-only in this plugin |
| `/v1/liveviews/{id}` | GET, PATCH | ✅ GET implemented, UNVERIFIED (issue #23); PATCH unbuilt |
| `/v1/files/{fileType}` | GET, POST | no |
| `/v1/alarm-manager/webhook/{id}` | POST | ✅ (issue #21) |
| `/v1/subscribe/events` | GET (WS upgrade) | ✅ the only motion source |
| `/v1/subscribe/devices` | GET (WS upgrade) | ✅ issue #18 — see below |
| `/v1/users` *(undocumented)* | GET | no |
| `/v1/bridges` *(undocumented)* | GET | no — returns `[]` on reference rig |

That's the 25 paths in the spec, plus the two undocumented ones below.

**`/v1/users` and `/v1/bridges` — undocumented in the vendored spec, verified
live.** Same treatment as `GET /nvrs`'s `armMode` (see below): observed on
the real controller, absent from v6.2.83. Verified 2026-08-31 on 7.2.105:
`GET /v1/users` → HTTP 200, a JSON array of user objects (`{id, name,
firstName, lastName, email, ucoreUserId, modelKey: "user"}`); `GET
/v1/bridges` → HTTP 200 `[]` on the reference rig. `bridge` appears in
v6.2.83 only inside the `/subscribe/devices` device union (schema: `id`,
`modelKey`, `state`, `name`, `mac`) — there is no `/v1/bridges` path. There
is no `user` object schema either (only a `userId` string used by
`liveview.owner`). Whether these were pulled from the documented surface in
v6.2.83, or the spec has simply never listed them, is unknown; treat their
shape as provisional, the same caveat as `armMode`.

## Event types on `/subscribe/events`

From the spec's `event` schema (a `oneOf` of 16 event shapes) — this is the
shape of the WS frame's `item`, not the frame itself. The wire frame is the
envelope `{"type": "add"|"update", "item": <event>}` (see
[`CONTRACT.md`](./CONTRACT.md) for a captured example). Common `item` shape:

```json
{"id": "...", "modelKey": "event", "type": "<one of below>",
 "start": <epoch ms>, "end": <epoch ms>|null, "device": "<device id>"}
```

`end` is optional **and** nullable in the spec — it is never in a type's
`required` list, and it is simply absent on `add` frames (the event hasn't
ended yet when it's created). That's the distinction `lightMotion`'s "no
`end` at all" below is contrasted against: every other type *can* carry
`end` (missing on `add`, `number` or `null` on `update`); `lightMotion` never
has the property, on either frame type.

Types: `motion`, `smartDetectZone`, `smartDetectLine`, `smartDetectLoiterZone`,
`smartAudioDetect`, `ring`, `sensorMotion`, `sensorOpened`, `sensorClosed`,
`sensorWaterLeak`, `sensorAlarm`, `sensorTamper`, `sensorBatteryLow`,
`sensorExtremeValues`, `sensorSmokeTest`, `lightMotion`.

Additions to the common shape, per type:

- `smartDetectZone`, `smartDetectLine`, `smartDetectLoiterZone`,
  `smartAudioDetect` add `smartDetectTypes` (array of string, or `null`).
- `sensorAlarm` adds `metadata.alarmType.text`, enum
  `smoke, CO, glassBreak`.
- `sensorExtremeValues` adds `metadata.sensorType.text` (enum
  `temperature, light, humidity`), `metadata.sensorValue.text` (number), and
  `metadata.status.text` (enum `neutral, low, safe, high, unknown`).
- `sensorWaterLeak`, `sensorOpened`, `sensorClosed` add
  `metadata.sensorMountType.text` (enum `door, window, garage, leak, none`).
- `sensorBatteryLow` adds `metadata.sensorBatteryPercentage.number`.
- `lightMotion` has **no `end` field at all** in the spec — it is a fire-once
  signal, not a start/stop pair like the others.
- `motion`, `ring`, `sensorMotion`, `sensorTamper`, `sensorSmokeTest` carry no
  additional fields.

**Observed live on the reference rig so far: only `smartDetectZone` and
`smartAudioDetect`.** Everything else above is spec-documented, not
witnessed — the rig has no doorbell, no Protect sensors, no floodlights.

`smartDetectTypes` object enum (from `cameraFeatureFlags.smartDetectTypes`
and the zone/line/loiter event schemas): `person, vehicle, package,
licensePlate, face, animal`. The reference cameras (2× UVC G5 Turret Ultra, 1× UVC G5 Bullet — all three advertise the same sets)
advertise `person, vehicle, animal` in `featureFlags.smartDetectTypes` (no
package, license plate, or face support on this hardware).

`smartAudioDetect` audio enum (from `cameraFeatureFlags.smartDetectAudioTypes`):
`alrmSmoke, alrmCmonx, alrmSiren, alrmBabyCry, alrmSpeak, alrmBark,
alrmBurglar, alrmCarHorn, alrmGlassBreak`. The reference cameras advertise
`alrmSmoke, alrmCmonx, alrmBabyCry, alrmSpeak`.

## `/subscribe/devices` WebSocket — VERIFIED 2026-08-31 on 7.2.105

Same handshake as `/subscribe/events`: `X-API-KEY` header, 101 response,
plain-text JSON frames (opcode 0x1, no binary/deflate framing).

Live test: a `PATCH ledSettings.isEnabled` on a camera produced exactly one
`update` frame, whose `item` held only `id`, `modelKey`, and `ledSettings` —
not the full camera object. An unrelated `bridge` device on the same rig
emitted its own `update` frame in the same window, with `item` holding only
`id`, `modelKey`, and `guid`. The partial-ness is **top-level only**: in the
capture, `ledSettings` arrived as the *whole* sub-object (`isEnabled`,
`welcomeLed`, `floodLed`), even though only `isEnabled` was patched. So the
frame carries only the top-level keys that changed, plus `id`/`modelKey` for
routing — but a nested settings object arrives whole, not diffed down to the
one field that actually moved.

The spec also defines `add` and `remove` frames:

- `add` — full object for the created device.
- `remove` — a bare reference: just `id` and `modelKey`.
- `update` — a "partial with reference": `id`, `modelKey`, plus whichever
  fields changed (as observed above).

All three frame types are a `oneOf` across every device `modelKey` the spec
knows about: `nvr, camera, chime, light, viewer, speaker, bridge, sensor,
aiprocessor, aiport, linkstation`.

**Used by the plugin since issue #18** (v2026.8.0) as a second, independent
push source alongside `/subscribe/events`: a `runConcurrentThread`-owned
`ProtectEventSocket` instance (the same class, just a different `path`/
`label`) is opened at the END of `_open_socket` — after the initial poll,
not "right after" the events socket handshake — polled in the same
`_pump()` tick, and routed through `device_router.py`
(`DeviceUpdateRouter.route()` + `merge_update()`) into the existing
camera/sensor/light/chime/nvr caches — see `docs/CONTRACT.md`'s "second
socket" section for the full design. It replaces the previous need to wait
for the 60s poll (issue #8) to see a config/state change (e.g. a status LED
flipped from the UniFi app, or a camera's `state` transitioning) — a
successful `update`/`add`/`remove` frame updates Indigo state immediately.

It does **not** replace the poll: chimes and the NVR have no other live
feed, and the socket's own retry/backoff is independent of (and may be
down while) the poll keeps working -- the poll surviving a device-socket
outage is the durable justification, not a verified claim about what the
socket pushes. (Whether `/subscribe/devices` ever pushes a sensor/light
`stats`/measurement change has NOT been verified against real hardware --
the reference rig's `/sensors`/`/lights` are both empty, so there is
nothing to capture against; treat "sensors/lights still need the poll for
measurements" as unconfirmed, not as a proven wire fact.) It also does
**not** affect
`connected` or camera motion in any way — those remain entirely driven by
`/subscribe/events`; losing the device socket only means config/state
states go back to being poll-only (60s) freshness until it reconnects.

## Writes — VERIFIED 2026-08-31 on 7.2.105

`PATCH /cameras/{id}` works with the API key alone — no session/password
auth needed for this write. Partial bodies are accepted; the response is the
full updated camera object.

Fields verified writable: `ledSettings.isEnabled`, `osdSettings.isDateEnabled`,
`videoMode`, `hdrType`, `micVolume`, `name`.

Error shapes:

- Invalid enum/type value, or an unknown field — HTTP 400:
  ```json
  {"error": "Failed to parse 'request-body'", "name": "AJV_PARSE_ERROR",
   "issues": [{"instancePath": "/videoMode",
               "message": "must be equal to one of the allowed values",
               "keyword": "enum"}],
   "isUserError": true}
  ```
  `additionalProperties` is rejected by the schema, so a typo'd key is a 400,
  not a silent no-op.
- Unknown camera id — HTTP 404:
  ```json
  {"error": "Entity 'camera' not found", "name": "NOT_FOUND"}
  ```

This answers the open question in issue #6 ("whether the API key alone
authorises writes, or whether some writes need the private API") for the
camera fields tested: it does. Mic-disable and talkback writes are still
spec-documented but untested. PTZ goto/patrol (issue #19) and the
alarm-manager webhook (issue #21) are now implemented in `protect_api.py`
(`ptz_goto`/`ptz_patrol_start`/`ptz_patrol_stop`/`send_alarm_webhook`) but
likewise UNVERIFIED against the reference rig, which has neither a PTZ
camera nor an Alarm Manager alarm configured — the API key alone
authorising them is assumed by extension from the camera PATCH result
above, not independently confirmed.

**`/ptz/goto/{slot}`'s own spec contradicts itself on the slot range.**
Its prose says "slot 0-4", identically to `/ptz/patrol/start/{slot}`, but
its own `examples` field for the goto endpoint lists `["-1","0","2","8",
"9"]` — reaching 9, which the prose says is illegal. `ptz_patrol_start`
has no such contradiction (`activePatrolSlotString` is a genuine 5-value
enum). `protect_api.ptz_goto` was widened to accept 0-9 accordingly (a
slot the camera doesn't actually have is refused by the controller, not
by this client); `ptz_patrol_start` stays 0-4. `PTZ_PRESET_SLOT_MAX`/
`PTZ_PATROL_SLOT_MAX` in `protect_api.py` are the single source of truth
both `plugin.py`'s ConfigUI menus and this contradiction note derive from.

## RTSPS streams

`GET /cameras/{id}/rtsps-stream` → `{"high": <url>|null, "medium": <url>|null,
"low": <url>|null, "package": <url>|null}`, each a
`rtsps://<host>:7441/<token>?enableSrtp` URL.

Verified 2026-08-31: the token was **identical** across two `GET`s 10s apart.
Rotation over longer windows (hours/days, or across controller reboot) is
untested. `package` is `null` on the reference cameras (`hasPackageCamera:
false` — itself undocumented, see "Where the truth lives" above: it's on the
live camera object and in `CONTRACT.md`, but not in the spec's `camera`
schema).

The spec also documents `POST` (body `{"qualities": [...]}`, `qualities` has
`minItems: 1` — creates streams, implemented as `create_rtsps_streams`,
issue #7) and `DELETE` (takes `qualities` as a **query** parameter, not a
body — `anyOf` an array or a single value of `high|medium|low|package` —
and returns **204** on success) on the same path.

`DELETE` is implemented (issue #25) as `protect_api.delete_rtsps_stream`,
UNVERIFIED against the reference rig, and deliberately sends only the
single-value form — one quality per call (`?qualities=high`), never the
array form. How this server expects an array encoded into a query string
(repeated keys? comma-joined? bracketed `qualities[]=`?) is undocumented,
and the single-value form is unambiguous and already a documented option,
so there was nothing to gain by guessing at the other one. A caller
deleting several qualities makes several calls. Every `ProtectAPIError`
this raises has its body redacted the same way `get_rtsps_streams`/
`create_rtsps_streams` already do — this endpoint family can echo a
stream URL/token back in an error response.

**Treat the URL as a credential** — the token in the path *is* the auth, per
issue #7. Don't log it, and think before writing it into a device state that
anything with DB access can read.

## `GET /nvrs`

The spec's `nvr` schema documents only:

```json
{"id": "...", "modelKey": "nvr", "name": "...",
 "doorbellSettings": {"defaultMessageText": "...",
                       "defaultMessageResetTimeoutMs": 0,
                       "customMessages": [], "customImages": []}}
```

The **live** 7.2.105 response also carries `type`, `guid`, `mac`, and an
**undocumented** `armMode` object:

```json
"armMode": {"status": "disabled", "armedAt": null, "willBeArmedAt": null,
            "breachDetectedAt": null, "breachEventCount": 0,
            "breachTriggerEventId": null, "breachEventId": null}
```

`armMode` is **observed, not documented** — it is not in the v6.2.83 spec at
all. Treat its shape as provisional; it could change without a spec bump
since Ubiquiti isn't committing to it in writing. `status` reads `"disabled"`
on the reference rig (Protect's own arm/disarm system is off there).

## What the official API cannot provide vs the private API

Full background: [issue #9](https://github.com/simons-plugins/indigo-unifi-protect/issues/9).

Compared against the private API (`/proxy/protect/api/…`,
`/proxy/protect/ws/updates`, username/password session auth — used by e.g.
kw123's `uniFiAP` plugin), the official integration API does **not** expose:

- `firmwareVersion`
- camera IP address
- `isDark` (day/night state) **for cameras** — but lights *do* expose
  `isDark` (`light.isDark`, confirmed in the spec's `light` schema)
- `irLedMode` / `irLedLevel`
- `zoomPosition`, pan/tilt position (PTZ *commands* do exist — patrol
  start/stop, goto preset — the position readback doesn't)
- motion tuning (`motionAlgorithm`, `motionRecordingMode`, pre/post padding)
- wifi signal stats
- smart-detect `confidence` and `zone` — `/subscribe/events` frames carry
  `smartDetectTypes` only, never a confidence score or the zone that
  triggered

The deliberate trade: getting those fields means adopting username/password
auth and the private binary WebSocket (8-byte header + zlib-deflated JSON) —
the auth model and wire format this plugin was specifically built to avoid.
API-key-only auth and plain-JSON frames are the whole reason `protect_ws.py`
can stay stdlib-only; see the README and `CONTRACT.md`.

## Rate limit

~5 req/s → 429; 3s spacing is clean. Already documented in
[`CONTRACT.md`](./CONTRACT.md#verified-facts-about-the-target-api) — not
repeated here.
