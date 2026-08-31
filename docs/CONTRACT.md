# Internal module contract — indigo-unifi-protect

**Every module in this plugin is written against this file.** Signatures here are
indicative, not binding to the letter: other modules are written in parallel
against them, so changing one breaks a sibling, but individual type
annotations in the code may be looser than shown here (e.g. a bare `set`
where this doc writes `set[str]`). If a signature genuinely cannot work, say
so in your report — do not silently change it.

Python **3.10+**, **stdlib only**. No `requests`, no `websockets`, no `aiohttp`.
Nothing goes in `Contents/Packages/`.

---

## Verified facts about the target API

Proven live on 2026-08-26 against a UNVR at `192.168.0.10`, UniFi Protect
**7.2.105**. Do not "fix" code to contradict these.

For the full endpoint and event-type inventory (all 25 paths, the vendored
OpenAPI spec, `/subscribe/devices`, write verification, and the official-vs-
private API gap), see [`API-REFERENCE.md`](./API-REFERENCE.md).

- Base URL: `https://<host>/proxy/protect/integration/v1`
- Auth: **`X-API-KEY: <key>` header alone.** No login, no cookies, no CSRF token.
  The same header authenticates the WebSocket upgrade.
- `GET /cameras` → JSON array. Camera keys observed:
  `activePatrolSlot, featureFlags, guid, hasPackageCamera, hdrType, id,
  isMicEnabled, ledSettings, mac, micVolume, modelKey, name, osdSettings,
  smartDetectSettings, state, type, videoMode`
  There is **no** `isMotionDetected` and **no** `lastMotion`.
- `GET /cameras/{id}/snapshot` → `image/jpeg`. `?highQuality=true` returns
  **400** `{"error":"Camera does not support full HD snapshot"}` when the
  camera's `featureFlags.supportFullHdSnapshot` is false.
- `GET /cameras/{id}/rtsps-stream` → `{"high":…,"medium":…,"low":…,"package":null}`
- `GET /events` → **404. This endpoint does not exist.**
- `WS /subscribe/events` → **opcode 0x1 plain-text JSON frames.** No binary
  framing, no zlib. (The *private* `/proxy/protect/ws/updates` API is binary +
  deflate — we are deliberately not using it.)
- **Rate limit (2026-08-26):** ~5 req/s earned HTTP 429; one request per 3s
  was clean. Nothing was measured in between. `plugin.py`'s `MIN_REST_INTERVAL`
  (3.0s) is set from this finding.

**Consequence: the WebSocket is the only motion source. There is no polling
fallback.** If the socket is down, motion is unknowable — say so in device
state rather than reporting "no motion".

### Event frame shape

```json
{"type": "add",
 "item": {"id": "9b5f3607-d8d6-4ddf-b17d-8cde369e4327",
          "modelKey": "event",
          "type": "smartDetectZone",
          "start": 1787756557629,
          "device": "69ed1b24002f7f03e407c90a",
          "smartDetectTypes": ["person"]}}
```

Lifecycle: `add` (has `start`, no `end`) → zero or more `update` keepalives →
`update` carrying `end`. Timestamps are **epoch milliseconds**.

Real capture in `tests/fixtures/ws_capture.json` (10 frames, two cameras, one
person). **Two traps it proves, both of which must be handled:**

1. **Terminal frames repeat.** One event's `end` arrived 3x, the other's 2x.
2. **A stale keepalive can arrive after/alongside the `end`.** Frame 8 is an
   `update` with **no** `end` for an event whose `end` lands in frame 9. Treated
   naively it re-arms the sensor and it latches on forever.

#### Two event families share this one socket

`item.type` distinguishes them; `event_tracker.py` must inspect it, and
originally did not — an audio "speech" event was folded into motion state
because nothing looked at `item.type` at all. Motion and audio are otherwise
identical in shape and lifecycle (add/update/end, epoch-ms timestamps, ids
unique across both families).

- **Motion**: `item.type` in `{"motion", "smartDetectZone", "smartDetectLine",
  "smartDetectLoiterZone"}` (empty `smartDetectTypes` for the plain `motion`
  type, which carries none). Only `smartDetectZone` has been observed on the
  reference rig; the other three come from Protect's published OpenAPI spec,
  not proven on the wire.
- **Audio**: `item.type` is `smartAudioDetect`.
- `smartDetectTypes` values: this plugin only TRACKS (surfaces as its own
  state) `person`, `vehicle`, `animal` for motion and `alrmSpeak`,
  `alrmBabyCry`, `alrmSmoke`, `alrmCmonx` for audio. Observed live: `person`
  and `alrmSpeak` only — nothing else in either list is proven on the wire.
  The spec's (v6.2.83) full enums are wider: motion objects are `person,
  vehicle, package, licensePlate, face, animal`; audio alarms are `alrmSmoke,
  alrmCmonx, alrmSiren, alrmBabyCry, alrmSpeak, alrmBark, alrmBurglar,
  alrmCarHorn, alrmGlassBreak`. Any value outside the tracked list still
  passes through into `lastDetectTypes`/`lastAudioTypes` untouched — it just
  never sets one of the specific boolean states, and for audio it still sets
  `audioDetected`.
- Any other `item.type` (including missing/non-string, and documented-but-
  unhandled types like doorbell `ring` or Protect sensor events) must be
  **ignored and counted**, never folded into either family — UNLESS the
  frame carries an `end` for an id this tracker is already holding, in which
  case it must still finish that event (see "Required semantics" below).

**Three wire facts, proven live on 2026-08-26 by speaking near a camera
(the "Side Path" camera, UNVR 7.2.105):**

1. Audio's `item.type` is `smartAudioDetect`.
2. The audio `add` frame carries an **EMPTY** `smartDetectTypes` — the
   actual classification (e.g. `alrmSpeak`) arrives on the first `update`,
   roughly a second later. This is not malformed; the event is "active but
   unclassified" in between.
3. Audio event ids are **24-char hex** (e.g. `6a8f49ac03d53b03e402a148`),
   not UUIDs like motion event ids.

Real capture in `tests/fixtures/ws_capture_audio.json` (13 frames: 10
`smartDetectZone` across the same two cameras as `ws_capture.json`,
interleaved with 3 `smartAudioDetect` frames for a third camera). Frames 3-5
are the audio add/classify/end sequence proving facts 1-2 above.

---

## `protect_api.py`

```python
class ProtectAPIError(Exception):
    """Raised for any non-2xx response or transport failure.

    Attributes:
        status (int|None), body (str), url (str)
        kind (str): coarse failure category derived from `status`, so
            callers can react without hardcoding numeric codes: "auth"
            (401/403), "not_found" (404), "rate_limited" (429),
            "bad_request" (400), "server" (5xx), "transport" (status is
            None), "http" (any other non-2xx status).
        retry_after (float|None): seconds to wait before retrying, parsed
            from the response's `Retry-After` header when present (most
            relevant for kind == "rate_limited"). None when absent or
            unparseable.
    """

class ProtectAPI:
    def __init__(self, host: str, api_key: str, verify_ssl: bool = False,
                 timeout: int = 15) -> None: ...

    def get_cameras(self) -> list[dict]:
        """GET /cameras. Raises ProtectAPIError."""

    def get_camera(self, camera_id: str) -> dict: ...

    def get_snapshot(self, camera_id: str, high_quality: bool = False,
                      supports_high_quality: bool | None = None) -> bytes:
        """Return raw JPEG bytes.

        Raises ProtectAPIError. If high_quality is requested:
        - When the caller knows the camera's capability (typically from
          its cached `featureFlags.supportFullHdSnapshot`), pass it as
          `supports_high_quality` to skip the wasted round trip when the
          camera can't do it.
        - When `supports_high_quality` is left None (unknown), fall back to
          requesting `highQuality` and retry ONCE without the flag if the
          server answers 400 mentioning "full hd" — but log a warning
          naming the camera.
        """

    def get_rtsps_streams(self, camera_id: str) -> dict: ...

    def get_meta_info(self) -> dict:
        """GET /meta/info -> {'applicationVersion': '7.2.105'}.
        Used as the connectivity check; cheapest authenticated call."""
```

Use `urllib.request` with an `ssl.SSLContext`. When `verify_ssl` is false set
both `check_hostname = False` and `verify_mode = ssl.CERT_NONE` (a UNVR serves a
self-signed cert; this is the normal case, not an error).

**Never log the API key**, including inside exception text or a repr.

---

## `protect_ws.py`

```python
class ProtectEventSocket:
    """Blocking, stdlib-only WebSocket client for /subscribe/events.

    Not thread-safe. One instance is driven by one thread.
    """

    def __init__(self, host: str, api_key: str, verify_ssl: bool = False,
                 logger=None) -> None: ...

    def connect(self) -> None:
        """Open TCP+TLS, perform the RFC6455 handshake with the X-API-KEY
        header, and validate the 101 response. Raises ConnectionError on
        anything other than '101'."""

    @property
    def last_frame_at(self) -> float | None:
        """Monotonic timestamp of the last WS frame received, of ANY opcode
        (including ping/pong/continuation), not just a decoded JSON message.
        Also set by connect(). None before connect()."""

    def send_ping(self) -> None:
        """Send a client PING frame. Raises ConnectionError if not connected
        or the send fails."""

    def read_message(self, timeout: float = 1.0) -> dict | None:
        """Return the next decoded JSON message, or None if the timeout
        elapsed with no complete frame (this is normal and not an error —
        it is how the caller stays responsive to shutdown).

        Internally loops, consuming and discarding ping (replying pong),
        pong, and continuation frames without returning — it keeps reading
        until a complete data message decodes or the timeout elapses; it
        does NOT return None for each such frame individually. Fragmented
        messages (FIN bit unset) are buffered and fully reassembled across
        CONTINUATION frames before being decoded — they are never discarded.
        Raises ConnectionError on a close frame or a dropped socket.
        """

    def close(self) -> None:
        """Idempotent. Safe to call on a never-connected instance."""
```

Client frames **must be masked** (RFC 6455 §5.3) — a server may drop the
connection otherwise. Server frames are unmasked. Handle payload lengths 126
(2-byte) and 127 (8-byte) extended forms. A read may return a partial frame;
buffer across `recv` calls.

**The server sends nothing on an idle socket.** Verified 2026-08-26: a
5-minute live capture saw 10 frames during activity, then 2 minutes of total
silence on a healthy connection. Silence is therefore not usable as a death
signal on its own — this is why `last_frame_at` and `send_ping()` exist: the
caller (`plugin.py`) pings periodically and treats "no frame of any kind
(including a pong) for N seconds despite pings" as the actual death signal,
not a plain "no frames for N seconds" watchdog.

Reference implementation of the handshake and frame reader that is **already
proven against this exact server**: `docs/ws_probe.py`. Lift from it.

---

## `event_tracker.py`

The heart of the plugin. Pure logic, no Indigo imports, no I/O — so it is
fully unit-testable.

```python
class EventTracker:
    """Folds the Protect event stream into per-camera MOTION and AUDIO
    state, tracked as two fully independent families."""

    def __init__(self, finished_cap: int = 512) -> None: ...

    def handle(self, message: dict) -> set[str]:
        """Apply one WS message. Return the set of camera ids whose derived
        state CHANGED as a result (empty set when nothing changed — which is
        the correct outcome for a duplicate or stale frame, or a frame of
        an item.type this tracker doesn't act on).

        A camera counts as changed when ANY of four independent things
        changed: the MOTION active/idle flag, the MOTION detect-type union,
        the AUDIO active/idle flag, or the AUDIO detect-type union. A camera
        with an active "person" event that then also picks up an active
        "vehicle" event does not flip active/idle, but its detect-type
        union did change, and the caller (which only writes Indigo states
        for cameras in the returned set) needs to know — otherwise a second
        concurrent detect type (e.g. vehicleDetected) never becomes True
        during an overlap with an already-active event. The same applies to
        audio: classification lands on the `update` after the `add`, not on
        the `add` itself, so speechDetected etc. depend on the type-union
        case firing, not just the active/idle case.

        Malformed messages (missing 'item', missing 'device', non-dict) are
        ignored and return an empty set. Never raise. A message whose
        `item.type` is not a recognized MOTION or AUDIO type (missing,
        non-string, or simply unhandled) is ALSO ignored and returns an
        empty set, but is counted separately (`ignored_type_counts`) rather
        than as malformed — the frame parsed fine, it just isn't a family
        this tracker acts on.
        """

    @property
    def malformed_count(self) -> int:
        """Count of messages that could not be parsed at all and were
        discarded (as opposed to tolerated/degraded and still processed, or
        ignored for an unrecognized item.type). Exists because an empty
        `handle()` return set alone can't distinguish "duplicate frame,
        nothing to do" (correct, expected) from "a frame was destroyed and
        something may have been lost" (a stream-health problem worth
        surfacing)."""

    @property
    def dropped_terminal_count(self) -> int:
        """Subset of `malformed_count` where the discarded frame's `item`
        carried an `end` — i.e. a lifecycle signal was actually lost, not
        just a keepalive. This is the one that matters: a camera could be
        stuck reporting active when its terminating frame was silently
        destroyed, and `malformed_count` alone doesn't tell the caller
        whether that's possible."""

    @property
    def ignored_type_counts(self) -> dict[str, int]:
        """Count of frames whose `item.type` was not a recognized MOTION or
        AUDIO type, keyed by the type string (`"<missing>"` when absent or
        not a string). NOT included in `malformed_count` — these frames
        parsed fine, they just aren't a family this tracker folds into
        state. plugin.py surfaces new keys here as a log line, once per
        type per run."""

    def is_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished MOTION event references this camera."""

    def detect_types(self, camera_id: str) -> set[str]:
        """Union of smartDetectTypes across this camera's active MOTION
        events. Empty set when idle."""

    def last_motion_ms(self, camera_id: str) -> int | None:
        """Epoch-ms `start` of the most recent MOTION event seen for this
        camera, active or not. None if never seen."""

    def audio_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished AUDIO event references this camera.
        True from the `add` onward, even before classification lands —
        an unclassified audio event still counts as active."""

    def audio_types(self, camera_id: str) -> set[str]:
        """Union of smartDetectTypes across this camera's active AUDIO
        events. Empty set when idle, and also empty for an active-but-not-
        yet-classified audio event."""

    def last_audio_ms(self, camera_id: str) -> int | None:
        """Epoch-ms `start` of the most recent AUDIO event seen for this
        camera, active or not. None if never seen."""

    def clear_camera(self, camera_id: str) -> bool:
        """Force a camera idle in BOTH families (used on socket loss).
        Returns True if EITHER family was active."""

    def reset(self) -> None:
        """Drop all state, both families. Used on reconnect."""
```

### Required semantics

1. `add` or `update` **without** `end` → the event is active, in its
   family, for `item.device` — **unless its id is already finished**, in
   which case ignore it entirely. This is trap 2 and it is the single most
   important line in the module. The finished-id cache is **shared** across
   both families: motion and audio event ids are already globally unique
   (UUIDs vs 24-char hex), so there is no need to key it per-family.
2. `update` **with** `end` → mark the id finished, remove from that
   family's active set. Record the id in a bounded structure (cap
   `finished_cap`, evict oldest) so repeats and late keepalives stay
   suppressed without unbounded growth.
3. A camera is active (per family) while it has ≥1 active event in that
   family. Two overlapping events on one camera, in the same family, must
   both clear before that family goes idle. The two families never
   interact: an active audio event never makes `is_active` true, and an
   active motion event never makes `audio_active` true.
4. `detect_types`/`audio_types` is the union over that family's active
   events only.
5. An `end` for an id never seen before still marks it finished (out-of-order
   arrival must not create an active event).
6. `item.type` selects the family BEFORE anything else runs: `{"motion",
   "smartDetectZone", "smartDetectLine", "smartDetectLoiterZone"}` →
   MOTION, `{"smartAudioDetect"}` → AUDIO, anything else → ignored and
   counted in `ignored_type_counts`, with no further processing — an `end`
   frame of an unrecognized type for an id never seen must not finish (or
   create) anything.
7. **Exception to #6**: an `end` frame for an id the tracker is CURRENTLY
   HOLDING (present in the shared `event_id -> (family, device)` active
   index) must still finish that event, regardless of what — or whether —
   `item.type` says. A real bug let a missing-type or wrong-family-typed
   `end` for a held id fall into the ignore-and-count branch and lose the
   lifecycle signal, leaving the camera stuck active forever with zero
   diagnostic. `_finish` resolves the family from the index, never from the
   terminating frame's own claim. This does NOT apply to an id never seen —
   that case is still #6 (ignored and counted, nothing created or finished).
8. `handle` returns changed-camera ids so the caller writes Indigo states only
   on real transitions — but "changed" is EITHER the active/idle flag OR the
   detect-type union moving, for EITHER family, not only active/idle
   transitions. (An earlier version only fired on active/idle transitions;
   that was a real bug — `vehicleDetected` never became True when a vehicle
   event overlapped an already-active person event on the same camera. A
   second, related bug folded audio events into motion state entirely,
   because `item.type` was never inspected at all.) A duplicate `end`
   still returns an empty set.

---

## `plugin.py`

Standard Indigo lifecycle. Key points:

- `__init__` — `super().__init__(...)` first, instance vars only, **no** Indigo
  DB access, **no** network.
- `startup()` — do **not** call `super().startup()`, it does not exist.
- `runConcurrentThread()` — owns the WS read loop directly. Do **not** spawn a
  second thread; Indigo already manages this one. Loop on
  `self.read_message(timeout=1.0)` so `self.StopThread` is honoured within ~1s.
  Use `self.sleep()`, never `time.sleep()`.
- Reconnect with exponential backoff 1s → 60s. On disconnect, set every camera
  device's `connected` state False and call `tracker.clear_camera` for each, so
  a dead socket reads as "unknown", not as "no motion".
- `deviceStartComm` / `deviceStopComm` maintain `self.cameras: dict[str, set[int]]`
  mapping Protect camera id → the set of Indigo device ids pointed at it (a
  set, not a scalar, because Indigo's Duplicate command trivially produces
  two devices on one camera).

### State IDs — strict, undocumented Indigo rule

State ids must be **ASCII letters and digits only, starting with a letter**.
Underscores are **forbidden** and fail with `LowLevelBadParameterError --
illegal XML tag name character`, which does not name the offending key. Use
camelCase. Do not add a state called `batteryLevel` (reserved; writes are
silently routed to the native property).

The declared states are exactly:

| State id | Type | Meaning |
|---|---|---|
| `motionDetected` | Boolean (OnOff) | MOTION family active (person/vehicle/animal/plain motion) |
| `personDetected` | Boolean | `person` in active MOTION detect types |
| `vehicleDetected` | Boolean | `vehicle` in active MOTION detect types |
| `animalDetected` | Boolean | `animal` in active MOTION detect types |
| `audioDetected` | Boolean | AUDIO family active — true even while unclassified |
| `speechDetected` | Boolean | `alrmSpeak` in active AUDIO detect types |
| `babyCryDetected` | Boolean | `alrmBabyCry` in active AUDIO detect types |
| `smokeAlarmDetected` | Boolean | `alrmSmoke` in active AUDIO detect types |
| `coAlarmDetected` | Boolean | `alrmCmonx` in active AUDIO detect types |
| `lastMotion` | String | ISO-8601 local time, `""` if never |
| `lastDetectTypes` | String | comma-joined MOTION types, e.g. `person,vehicle` |
| `lastAudio` | String | ISO-8601 local time, `""` if never |
| `lastAudioTypes` | String | comma-joined AUDIO types, e.g. `alrmSpeak` |
| `cameraState` | String | Protect's `state`, e.g. `CONNECTED` |
| `cameraModel` | String | camera object's `type`, e.g. `UVC G5 Turret Ultra` |
| `videoMode` | String | camera object's `videoMode` |
| `hdrType` | String | camera object's `hdrType` |
| `micEnabled` | Boolean | camera object's `isMicEnabled` |
| `micVolume` | Integer | camera object's `micVolume`; the key is skipped (not written) if it fails to parse as `int` |
| `ledEnabled` | Boolean | camera object's `ledSettings.isEnabled` |
| `osdNameEnabled` | Boolean | camera object's `osdSettings.isNameEnabled` |
| `osdDateEnabled` | Boolean | camera object's `osdSettings.isDateEnabled` |
| `connected` | Boolean | **event socket** health, not the camera's |
| `snapshotPath` | String | path written by the snapshot action |

The eight camera-info states above (issue #4) are read straight from the
cached `GET /cameras` object (`self.camera_info`), not from the WS tracker,
and their fate is entirely independent of `connected` / socket health — see
below for why that's a deliberate split from the motion/audio states.

They are **only written when the camera object itself is available** — when
the lookup has failed entirely, `cameraState` already reports
`STATE_UNAVAILABLE` and all eight are left at their last-known values rather
than overwritten with a made-up False/`""` that would look like a fresh,
confirmed read.

Within a present object, the rule is per-key, not per-object: **a key that
is absent or malformed is skipped, never defaulted.** This matters because
the camera object can be *partially* present — a read can return some
fields and not others — and a missing key is not the same claim as a
present-and-false one:
- The three string keys (`cameraModel`/`type`, `videoMode`, `hdrType`) are
  written only when present and non-empty; a missing one keeps whatever
  value that state already held, exactly like the whole-object-missing case
  above (and consistent with `dev.model`, which is likewise only ever
  updated, never cleared).
- The four boolean keys (`micEnabled`/`isMicEnabled`,
  `ledEnabled`/`ledSettings.isEnabled`,
  `osdNameEnabled`/`osdSettings.isNameEnabled`,
  `osdDateEnabled`/`osdSettings.isDateEnabled`) are written only when their
  source key is present. An absent key is skipped, not written as False —
  "mic disabled" is a real reading and must not be confused with "the field
  wasn't in the payload".
- `ledSettings`/`osdSettings` are guarded with `isinstance(x, dict)`: a
  truthy non-dict value there (a malformed object, not merely an absent
  one) must not raise `AttributeError` out of `_write_states` — that would
  escape to `_pump`, tear the socket down, and reconnect into the same bad
  object forever, one malformed camera killing motion for every camera.
- `micVolume` is parsed with `int()` inside a `try`; a `bool` is explicitly
  rejected before that (`int(True) == 1` is a real-looking but fabricated
  volume), and any failure — missing, wrong type, unparseable — skips the
  key rather than writing a fabricated `0`.

`dev.model` is set from `type` when it differs from the device's current
model, via `dev.replaceOnServer()`, guarded so a failure there is never
able to block the state write above it, which always happens first. Because
`indigo.devices.get()` returns a fresh device object on every call in real
Indigo, a *persistent* `replaceOnServer()` failure (e.g. the device's edit
dialog left open in the Indigo UI) would otherwise retry — and log — on
every single frame forever with nothing visible to say so. The plugin logs
the first failure per device id at WARNING and every one after that at
DEBUG, tracked in `self._model_update_warned`, the same one-per-key pattern
`_reported_ignored_types` already uses for unrecognized event types.

`onOffState` (the built-in on/off state) is `motionDetected` OR (the
per-device `audioCountsAsActivity` checkbox, default True, AND active AUDIO
types intersect `{alrmSpeak, alrmBabyCry}`). Smoke/CO alarm sounds NEVER
contribute to `onOffState`, checkbox or not — an alarm is not presence, and
folding it in would make a "device turned on" trigger fire on a smoke alarm.
When `connected` is False: the live booleans (`motionDetected`,
`audioDetected`, `onOffState`, the per-type booleans) and the `*Types`
strings (`lastDetectTypes`, `lastAudioTypes`) go False/empty.
`lastMotion`/`lastAudio` are historical timestamps, not live state, and are
KEPT — a disconnect does not erase when motion or audio was last actually
seen. The eight camera-info states (issue #4, above) are a second,
independent exception, for a different reason: they follow `camera_info`,
not `connected`, and keep their last-known values across a socket drop too
— a dead event socket says nothing about whether the camera's hardware
config has changed.

`onOffState`, `motionDetected`, `audioDetected` and every other state above
are written together in one batched call —
`dev.updateStatesOnServer([...])` — not via individual
`updateStateOnServer()` calls. The state image
(`MotionSensorTripped`/`MotionSensor`) tracks `onOffState`, not
`motionDetected` alone.

---

## Testing

`pytest`, mirroring `netro/tests/` (workspace standard: `unittest.mock`, the
`indigo` module stubbed in `conftest.py`).

`test_event_tracker.py` is the priority and must include, phrased
adversarially — **"when could this report idle and be wrong?"**:

- the real 10-frame capture replayed in order, asserting the exact
  active/idle transitions for both cameras
- duplicate `end` frames produce no second transition
- **a no-`end` keepalive arriving after that event's `end` does NOT re-arm**
- two overlapping events on one camera: both must end before it goes idle
- an `end` for an unknown id does not create an active event
- malformed messages (`{}`, missing `item`, `item` not a dict, missing
  `device`, missing `id`) are ignored and never raise
- a missing (or non-list, or mixed-type) `smartDetectTypes` is tolerated and
  DEGRADES to an empty/filtered set rather than discarding the frame — it is
  not in the malformed list above, because a bad cosmetic field must never
  be allowed to veto a lifecycle signal (`end`)
- `finished_cap` eviction does not resurrect a recently-ended event

Issue #5 (audio) added `tests/fixtures/ws_capture_audio.json` (13 frames: 10
`smartDetectZone` across the same two cameras as `ws_capture.json`,
interleaved with 3 `smartAudioDetect` frames for a third, "Side Path",
camera — captured 2026-08-26 by speaking near it), and must additionally
cover:

- the 13-frame capture replayed in order, asserting motion and audio never
  bleed into each other on any camera at any frame
- an unrecognized `item.type` (missing, non-string, or a documented-but-
  unhandled type like `ring`) never activates either family and is counted
  in `ignored_type_counts`, not `malformed_count`
- trap 2 applies identically to audio: a stale post-end audio keepalive
  does not re-arm `audio_active`
- a motion event and an audio event on one camera are fully independent:
  ending either leaves the other's state untouched
- `clear_camera`/`reset` clear both families
- `handle` reports changed when only the audio detect-type union moves
  (empty → `{alrmSpeak}`), mirroring the motion-family overlap fix
- `test_unknown_item_type_end_creates_or_finishes_nothing` — an unrecognized
  `item.type` on an `end` for an id never seen creates/finishes nothing
- `test_plain_motion_event_type_activates_motion_family_with_no_types` — the
  plain `motion` type (no smartDetectTypes at all) still activates MOTION
- `test_held_id_end_with_missing_type_still_finishes_the_event` and
  `test_held_id_end_with_other_family_type_still_finishes_the_event` — the
  exception to the ignore-and-count rule: an `end` for a HELD id must
  finish it regardless of what `item.type` says (see "Required semantics")

Per workspace convention, a degradation-path test must make the negative
assertion **fatal**: to prove the tracker never consults the network, hand it a
collaborator that raises if touched.
