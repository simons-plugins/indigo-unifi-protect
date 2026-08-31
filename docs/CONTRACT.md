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
    """Folds the Protect event stream into per-camera motion state."""

    def __init__(self, finished_cap: int = 512) -> None: ...

    def handle(self, message: dict) -> set[str]:
        """Apply one WS message. Return the set of camera ids whose derived
        state CHANGED as a result (empty set when nothing changed — which is
        the correct outcome for a duplicate or stale frame).

        A camera counts as changed when EITHER of two independent things
        changed: its active/idle flag, OR the union of detect types
        (person/vehicle/animal/...) across its active events. A camera with
        an active "person" event that then also picks up an active "vehicle"
        event does not flip active/idle, but its detect-type union did
        change, and the caller (which only writes Indigo states for cameras
        in the returned set) needs to know — otherwise a second concurrent
        detect type (e.g. vehicleDetected) never becomes True during an
        overlap with an already-active event.

        Malformed messages (missing 'item', missing 'device', non-dict) are
        ignored and return an empty set. Never raise.
        """

    @property
    def malformed_count(self) -> int:
        """Count of messages that could not be parsed at all and were
        discarded (as opposed to tolerated/degraded and still processed).
        Exists because an empty `handle()` return set alone can't
        distinguish "duplicate frame, nothing to do" (correct, expected)
        from "a frame was destroyed and something may have been lost"
        (a stream-health problem worth surfacing)."""

    @property
    def dropped_terminal_count(self) -> int:
        """Subset of `malformed_count` where the discarded frame's `item`
        carried an `end` — i.e. a lifecycle signal was actually lost, not
        just a keepalive. This is the one that matters: a camera could be
        stuck reporting active when its terminating frame was silently
        destroyed, and `malformed_count` alone doesn't tell the caller
        whether that's possible."""

    def is_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished event references this camera."""

    def detect_types(self, camera_id: str) -> set[str]:
        """Union of smartDetectTypes across this camera's active events.
        Empty set when idle."""

    def last_motion_ms(self, camera_id: str) -> int | None:
        """Epoch-ms `start` of the most recent event seen for this camera,
        active or not. None if never seen."""

    def clear_camera(self, camera_id: str) -> bool:
        """Force a camera idle (used on socket loss). Returns True if it
        was active."""

    def reset(self) -> None:
        """Drop all state. Used on reconnect."""
```

### Required semantics

1. `add` or `update` **without** `end` → the event is active for `item.device`
   — **unless its id is already finished**, in which case ignore it entirely.
   This is trap 2 and it is the single most important line in the module.
2. `update` **with** `end` → mark the id finished, remove from active. Record
   the id in a bounded structure (cap `finished_cap`, evict oldest) so repeats
   and late keepalives stay suppressed without unbounded growth.
3. A camera is active while it has ≥1 active event. Two overlapping events on
   one camera must both clear before it goes idle.
4. `detect_types` is the union over active events only.
5. An `end` for an id never seen before still marks it finished (out-of-order
   arrival must not create an active event).
6. `handle` returns changed-camera ids so the caller writes Indigo states only
   on real transitions — but "changed" is EITHER the active/idle flag OR the
   detect-type union moving, not only active/idle transitions. (An earlier
   version only fired on active/idle transitions; that was a real bug —
   `vehicleDetected` never became True when a vehicle event overlapped an
   already-active person event on the same camera.) A duplicate `end`
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
- `deviceStartComm` / `deviceStopComm` maintain `self.cameras: dict[str, int]`
  mapping Protect camera id → Indigo device id.

### State IDs — strict, undocumented Indigo rule

State ids must be **ASCII letters and digits only, starting with a letter**.
Underscores are **forbidden** and fail with `LowLevelBadParameterError --
illegal XML tag name character`, which does not name the offending key. Use
camelCase. Do not add a state called `batteryLevel` (reserved; writes are
silently routed to the native property).

The declared states are exactly:

| State id | Type | Meaning |
|---|---|---|
| `motionDetected` | Boolean (OnOff) | mirrors `onState` |
| `personDetected` | Boolean | `person` in active detect types |
| `vehicleDetected` | Boolean | `vehicle` in active detect types |
| `animalDetected` | Boolean | `animal` in active detect types |
| `lastMotion` | String | ISO-8601 local time, `""` if never |
| `lastDetectTypes` | String | comma-joined, e.g. `person,vehicle` |
| `cameraState` | String | Protect's `state`, e.g. `CONNECTED` |
| `connected` | Boolean | **event socket** health, not the camera's |
| `snapshotPath` | String | path written by the snapshot action |

Set `onState` via `dev.updateStateOnServer("onOffState", value=<bool>)` and keep
`motionDetected` in step. Batch multi-state writes with
`dev.updateStatesOnServer([...])`.

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
  `device`, missing `smartDetectTypes`) are ignored and never raise
- `finished_cap` eviction does not resurrect a recently-ended event

Per workspace convention, a degradation-path test must make the negative
assertion **fatal**: to prove the tracker never consults the network, hand it a
collaborator that raises if touched.
