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
- **`PATCH /cameras/{id}`, verified live 2026-08-31 on 7.2.105 — the
  plugin's first write path (issue #6).** `X-API-KEY` alone authorises it,
  same as every GET; no separate write credential. Partial JSON bodies are
  accepted. The response is the **full** camera object (same shape as
  `GET /cameras/{id}`), not just the changed fields — callers should use it
  to refresh their cache rather than issuing a follow-up GET. Verified
  writable fields: `ledSettings.isEnabled` (bool), `osdSettings.isNameEnabled`
  / `isDateEnabled` / `isLogoEnabled` (bool), `osdSettings.overlayLocation`
  (enum `topLeft|topMiddle|topRight|bottomLeft|bottomMiddle|bottomRight`),
  `videoMode` (must be one of that camera's own `featureFlags.videoModes`;
  spec enum `default|highFps|sport|slowShutter|lprReflex|lprNoneReflex`),
  `hdrType` (`auto|on|off`), `micVolume` (0–100 int), `name`.
  - A bad enum/type/unknown field → **400** with body
    `{"error":"Failed to parse 'request-body'","name":"AJV_PARSE_ERROR",
    "entity":"request-body","issues":[{"instancePath":"/videoMode",
    "message":"must be equal to one of the allowed values","keyword":"enum"}],
    "body":{...},"isUserError":true}`. `additionalProperties` is rejected.
  - An unknown camera id → **404** `{"error":"Entity 'camera' not found",
    "name":"NOT_FOUND"}`.
  - Camera `featureFlags` carries `hasLedStatus`, `hasHdr`, `hasMic`,
    `hasSpeaker`, `videoModes` — the write actions gate on these rather than
    assuming support; one camera on the reference rig reports
    `hasSpeaker: false`.

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
    """Raised for any non-2xx response or transport failure -- or, since
    the #6 review, for a 2xx whose body isn't trustworthy (kind="shape").

    Attributes:
        status (int|None), body (str), url (str)
        kind (str): coarse failure category, so callers can react without
            hardcoding numeric codes. Derived from `status` by default:
            "auth" (401/403), "not_found" (404), "rate_limited" (429),
            "bad_request" (400), "server" (5xx), "transport" (status is
            None), "http" (any other non-2xx status). A constructor `kind`
            argument overrides the derived value -- used for "shape" (a
            2xx response that parsed fine but isn't recognizably the
            object it claims to be; `status` is None there too, since it
            isn't an HTTP failure).
        retry_after (float|None): seconds to wait before retrying, parsed
            from the response's `Retry-After` header when present (most
            relevant for kind == "rate_limited"). None when absent or
            unparseable.
    """

    @property
    def issues(self) -> list[str]:
        """Field-level AJV validation issues from a 400 body, as
        "<instancePath>: <message>" strings (instancePath defaults to "/").
        Lazily parsed from `body` on every access. Falls back to `[error]`
        when there's no `issues` list but `body` has a string `error` field
        (e.g. a 404's `{"error":"Entity 'camera' not found"}`) -- the
        controller's own one-line explanation is worth surfacing even
        outside the AJV shape. [] only when `body` isn't JSON, or has
        neither `issues` nor `error`."""

class ProtectAPI:
    def __init__(self, host: str, api_key: str, verify_ssl: bool = False,
                 timeout: int = 15) -> None: ...

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 body: dict | None = None) -> bytes:
        """Shared HTTP core for every call below. `body`, when given, is
        JSON-encoded with Content-Type/Accept: application/json headers.
        `X-API-KEY` is the only auth, on every method including PATCH.
        `_get` is a thin wrapper over this with no body -- existing GET
        callers are unaffected by this method existing."""

    def get_cameras(self) -> list[dict]:
        """GET /cameras. Raises ProtectAPIError."""

    def get_camera(self, camera_id: str) -> dict: ...

    def patch_camera(self, camera_id: str, body: dict) -> dict:
        """PATCH /cameras/{id}. `body` is a partial camera object (issue
        #6) -- see "Verified facts" above for the fields proven writable.
        Returns the FULL camera object from the response, same shape as
        get_camera, so a caller can replace its cached copy with it
        directly. Raises ProtectAPIError, including when the response
        isn't a JSON object -- mirrors get_camera -- AND when it IS a dict
        but doesn't look like the real camera object (checked as
        `parsed.get("id") == camera_id and
        isinstance(parsed.get("featureFlags"), dict)`), raised with
        kind="shape" so a caller can react distinctly from an actual
        400/404 refusal. Without this second check, a 200 like
        `{"id": "cam-1"}` -- a proxy wrapper with none of the real fields --
        would be accepted and cached, blanking every hardware state.
        On a refusal, the server's AJV issues (if any) are on the raised
        error's `.issues`."""

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

    def get_rtsps_streams(self, camera_id: str) -> dict:
        """GET /cameras/{id}/rtsps-stream ->
        {"high": url|None, "medium": url|None, "low": url|None, "package": url|None}.
        Verified live 2026-08-31: the token was identical across two calls
        10s apart; rotation over a longer window is untested. `package` is
        null unless the camera reports `hasPackageCamera`.

        EVERY ProtectAPIError raised here -- from `_get_json` (an HTTP
        error/transport failure, whose `.body` `_request` would otherwise
        fill with the real response text) or this method's own shape check
        -- is re-raised through the module-level `_redact_body(exc)` helper,
        which copies the exception with `body=""` and everything else
        (including `kind`) preserved. This endpoint's response can contain
        a live-stream URL (an access token), and a malformed/error body
        could plausibly echo one back."""

    def create_rtsps_streams(self, camera_id: str, qualities: list[str]) -> dict:
        """POST /cameras/{id}/rtsps-stream with body {"qualities": [...]}
        (each entry one of "high"/"medium"/"low"/"package"), via `_request`.
        CREATES streams for the requested qualities and returns the same
        four-key object GET returns. Unverified against the reference rig
        -- GET already returned non-null URLs there.

        Same `_redact_body` treatment as get_rtsps_streams above, on every
        ProtectAPIError this raises (the `_request` call, the JSON parse,
        and this method's own shape check)."""

    def get_meta_info(self) -> dict:
        """GET /meta/info -> {'applicationVersion': '7.2.105'}.
        Used as the connectivity check; cheapest authenticated call."""

    def _request_no_content(self, method: str, path: str, params: dict[str, str] | None = None,
                             body: dict | None = None) -> None:
        """Shared core for the 204-No-Content endpoints below. Calls
        `_request` and discards the return value -- any 2xx is success
        regardless of body content; a stray non-empty body on a declared-204
        endpoint is never parsed and never a failure."""

    def ptz_goto(self, camera_id: str, slot: int) -> None:
        """POST /cameras/{id}/ptz/goto/{slot} (issue #19). `slot` is an int
        0-4 (Protect's own UI numbers the same five presets 1-5); bool is
        explicitly rejected (`isinstance(True, int)` is True in Python).
        Raises ValueError before any network call on an invalid slot.
        204 No Content on success -- fire-and-forget, there is no PTZ
        position readback anywhere in this API. SPEC-DERIVED, UNVERIFIED --
        the reference rig has no PTZ camera."""

    def ptz_patrol_start(self, camera_id: str, slot: int) -> None:
        """POST /cameras/{id}/ptz/patrol/start/{slot} (issue #19). Same
        slot contract and caveats as `ptz_goto`."""

    def ptz_patrol_stop(self, camera_id: str) -> None:
        """POST /cameras/{id}/ptz/patrol/stop (issue #19). No slot -- stops
        whichever patrol is currently running."""

    def send_alarm_webhook(self, trigger_id: str) -> None:
        """POST /alarm-manager/webhook/{id} (issue #21). `trigger_id` is a
        user-defined free-form string set up on the controller (Alarm
        Manager > alarm > Webhook trigger), not a Protect object id --
        URL-encoded with `urllib.parse.quote(trigger_id, safe="")` since it
        may contain "/" or spaces. Raises ValueError before any network
        call if empty/whitespace/non-str. 204 No Content on success.
        SPEC-DERIVED, UNVERIFIED -- the reference rig has no Alarm Manager
        alarms configured."""

    def delete_rtsps_stream(self, camera_id: str, quality: str) -> None:
        """DELETE /cameras/{id}/rtsps-stream?qualities=<quality> (issue
        #25). ONE quality per call, deliberately -- the spec's `qualities`
        query param is `anyOf` a single string or an array, but how the
        array form is expected to be encoded in a query string is
        undocumented for this server, and the single-string form is
        unambiguous and already documented, so this never guesses at the
        other encoding. Raises ValueError before any network call if
        `quality` isn't one of "high"/"medium"/"low"/"package". Same
        `_redact_body` treatment as get_rtsps_streams/create_rtsps_streams
        above on every ProtectAPIError raised -- this endpoint family can
        echo a stream URL/token back in an error body. 204 No Content on
        success. SPEC-DERIVED, UNVERIFIED."""
```

Use `urllib.request` with an `ssl.SSLContext`. When `verify_ssl` is false set
both `check_hostname = False` and `verify_mode = ssl.CERT_NONE` (a UNVR serves a
self-signed cert; this is the normal case, not an error).

**Never log the API key**, including inside exception text or a repr.

**Never log an RTSPS stream URL either** (issue #7) -- it embeds an access
token, exactly like the API key is a credential. This applies in both
`protect_api.py` (`get_rtsps_streams` and `create_rtsps_streams` both
redact EVERY ProtectAPIError they raise to `body=""` via `_redact_body`,
above) and `plugin.py` (every log line `_refresh_stream_urls` emits --
success, warning, or error -- passes through `_assert_no_url_in_message`, checked
against both the fresh values from the controller AND the device's
current stored states, since the error path has no fresh response to
check).

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

## `device_router.py` (issue #18)

Pure logic, no Indigo imports, no I/O — same contract as `event_tracker.py`,
fully unit-testable. Classifies frames from the `/subscribe/devices`
WebSocket (see `docs/API-REFERENCE.md`, "`/subscribe/devices` WebSocket —
VERIFIED 2026-08-31" for the wire facts this module is built against).

```python
HANDLED_MODEL_KEYS = frozenset({"camera", "sensor", "light", "chime", "nvr"})

class DeviceUpdateRouter:
    def route(self, message) -> tuple[str, str, str, dict] | None:
        """(kind, model_key, device_id, item) for a well-formed frame whose
        `type` is add/update/remove AND whose `item.modelKey` is in
        HANDLED_MODEL_KEYS AND `item.id` is a non-empty string. None
        otherwise. Never raises on any input."""

    @property
    def malformed_count(self) -> int: ...
        # message/item not a dict, missing/empty/non-string id, or the
        # message's own `type` missing/not one of add/update/remove.

    @property
    def ignored_model_counts(self) -> dict: ...
        # well-formed frames whose modelKey isn't handled (viewer, speaker,
        # bridge, aiprocessor, aiport, linkstation, or absent/non-string --
        # "<missing>"), keyed by modelKey. NOT malformed -- the frame
        # parsed fine, it just isn't a class this plugin has a device type
        # for. Mirrors event_tracker.ignored_type_counts so plugin.py can
        # surface new keys once per run the same way.

def merge_update(cached: dict, item: dict) -> dict:
    """A NEW dict: `cached` with `item`'s top-level keys overwriting.
    Never mutates either argument. Never raises -- a non-dict `item`
    degrades to an unchanged copy of `cached`."""
```

**The one fact this module exists to encode:** an `update` frame is
partial **top-level only**. `item` carries `id`/`modelKey` plus whichever
top-level keys actually changed, but a nested settings object (e.g.
`ledSettings`) arrives **whole** — verified live 2026-08-31: a `PATCH
ledSettings.isEnabled` produced an `update` whose `ledSettings` held
`isEnabled`, `welcomeLed`, AND `floodLed`, though only `isEnabled` was
patched. `merge_update` is therefore a plain top-level `dict.update()`,
never a recursive/deep merge — deep-merging would keep a stale nested key
under a value the controller never actually sent in that frame. A test
pins this directly: a key present only in the CACHED nested dict must be
GONE after the merge.

`add` is a full object (unlike `update`); `remove` is a bare
`id`+`modelKey` reference. Both are handled by `plugin.py`, not this
module — `device_router.py` only classifies the frame envelope, it never
touches a cache itself.

---

## `plugin.py`

Standard Indigo lifecycle. Key points:

- `__init__` — `super().__init__(...)` first, instance vars only, **no** Indigo
  DB access, **no** network.
- `startup()` — do **not** call `super().startup()`, it does not exist.
- `runConcurrentThread()` — owns the WS read loop directly. Do **not** spawn a
  second thread; Indigo already manages this one. `_pump()` drives BOTH the
  events socket and the device socket (issue #18) from that one thread:
  each ties up to `timeout=0.5` per tick (halved from the original 1.0s so
  the combined per-tick budget stays ~1s), so `self.StopThread` is still
  honoured within ~1s overall. Use `self.sleep()`, never `time.sleep()`.
- Reconnect with exponential backoff 1s → 60s. On disconnect, set every camera
  device's `connected` state False and call `tracker.clear_camera` for each, so
  a dead socket reads as "unknown", not as "no motion".
- `deviceStartComm` / `deviceStopComm` maintain `self.cameras: dict[str, set[int]]`
  mapping Protect camera id → the set of Indigo device ids pointed at it (a
  set, not a scalar, because Indigo's Duplicate command trivially produces
  two devices on one camera).
- `deviceStartComm` must call `stateListOrDisplayStateIdChanged()` before its
  first state write — Indigo does not add new Devices.xml states to existing
  devices otherwise.

### Second socket (issue #18): `/subscribe/devices`

`runConcurrentThread` is still the **only** thread — the device socket is
pumped from the same `_pump()` tick as the events socket, not a second
thread. `ProtectEventSocket` gained `path`/`label` constructor kwargs
(defaulting to the original events-socket values, so every existing caller
is byte-identical) so the same class serves both sockets.

- **`connected` means the EVENTS socket only, on purpose.** `_is_connected()`
  and `_mark_all_disconnected()` were deliberately left untouched — motion
  validity depends solely on `/subscribe/events`. The device socket only
  feeds freshness of poll-derived camera/sensor/light/chime/nvr config
  between polls; folding it into `connected` would make a healthy motion
  feed report itself unhealthy over an unrelated freshness-only outage.
- **Camera fallback is real (F1).** The device-socket-down WARNING promises
  "falling back to 60s polling for config/state freshness" — a promise that
  must hold for every class the device socket normally pushes, cameras
  included. `_poll_devices()` therefore also calls `_refresh_camera_info()`
  and re-applies every registered camera's state whenever `self.cameras`
  is non-empty AND `self.device_socket is None` — only while the device
  socket is actually down; when it's up, push already covers cameras and
  the extra `GET /cameras` would be pure waste. Each camera's re-apply is
  individually wrapped in `try`/`except`, the same "a poll bug must never
  kill the motion socket" rule `_apply_polled_write` already enforces for
  sensors/lights/chimes/NVR.
- **Independent retry/backoff, with a stability gate (F7).**
  `self._device_socket_retry_at` (monotonic gate) and
  `self._device_socket_backoff` (same `BACKOFF_START`→`BACKOFF_MAX` shape
  as the events socket) are separate state from the main reconnect loop.
  `_open_device_socket()` is best-effort and never raises: on failure it
  logs one WARNING per outage (`self._device_socket_warned`), sets the
  retry gate, and leaves `self.device_socket` None. `_pump()`'s own
  per-tick handling (`_pump_device_socket`/`_fail_device_socket`) reuses
  the exact same warned-flag/backoff state for a read/ping failure or a
  staleness timeout mid-session, so a socket that dies AFTER connecting
  logs exactly as many WARNINGs as one that never connected at all: one,
  per outage. Critically, a bare successful `connect()` does **not** reset
  `warned`/`backoff` any more — `self._device_socket_connected_at`
  (monotonic) is recorded instead, and `_pump_device_socket` only forgives
  (resets `warned=False`, `backoff=BACKOFF_START`, logs INFO "Device-update
  socket recovered") once a connection has stayed up for `>= STABLE_AFTER`.
  Without this gate, an accept-then-drop server produces a hot ~1s
  reconnect/WARNING/INFO loop forever — the exact failure `STABLE_AFTER`
  already exists to prevent on the events socket.
- **Uncached update is ignored, not seeded.** `update` only applies when
  the id is ALREADY in the relevant cache (`camera_info`/`sensor_info`/
  `light_info`/`chime_info`, or `nvr_info` for the NVR, keyed by
  `_nvr_known_id`). Merging an `update`'s partial fields onto nothing would
  fabricate a partial object that `_write_*_states` would then treat as a
  full, confirmed read. `add` has no such restriction — it IS a full
  object per spec, so it seeds the cache unconditionally and applies state
  only if the id is registered (Indigo has a device pointed at it);
  otherwise a DEBUG log ("new `<modelKey>` appeared on the controller") is
  the only trace. **`add` also clears any absence episode (F2/F3)** via
  `_clear_absent_from_list` — cameras have no list poll of their own to
  clear it the way sensors/lights/chimes self-heal within 60s, so without
  this a camera's remove→add→remove sequence would warn only once per
  plugin run. The NVR's `add` branch does the same for symmetry.
- **`remove` drops the cache entry and reuses `_warn_absent_from_list`'s
  once-per-absence-episode WARNING** when the id is registered — a
  removed camera then applies state exactly like any other "id absent from
  a successful poll" case: `cameraState`/`sensorState`/etc. read
  `STATE_UNAVAILABLE`, `connected` stays whatever the events socket says.
  **The NVR follows the same rule (F3)** — an NVR `remove` was previously
  silent; it now calls `_warn_absent_from_list("nvr", self._nvr_known_id or
  "nvr", self.nvrs)` when the NVR is registered, the same once-per-episode
  WARNING every other class gets.
- **Malformed frames are surfaced (F4).** `device_router.malformed_count`
  existed but was never read — a firmware envelope change could silently
  kill the whole feature while the socket looked healthy (frames keep
  refreshing `last_frame_at`). `_report_malformed_device_frames()` mirrors
  `_report_dropped_frames`: WARNING on the first increase this plugin run
  ("the device-config push may be broken; sensor/light/chime/NVR polling
  still applies and cameras fall back to 60s refresh"), DEBUG for every
  increase after that. Called from `_pump_device_socket` alongside
  `_report_ignored_models()` whenever `route()` returns `None`.
- **Broad exception containment at all three socket-lifecycle sites (F5).**
  `_open_device_socket`'s connect and both `_pump_device_socket` sites
  (`read_message`, `send_ping`) now catch `Exception`, not just
  `ConnectionError` — a non-`ConnectionError` defect (protect_ws's
  deliberate `_assert_no_secret` `AssertionError`, or a future
  `struct.error` in the shared frame parser) must never escape and be
  mistaken for an EVENTS-socket fault by `runConcurrentThread`'s generic
  handler, which would tear the healthy motion feed down. Mirrors
  `device_router.route()`'s own belt-and-braces rationale: contain and
  surface, don't crash the motion feed. Each generic-exception branch logs
  `type(exc).__name__` plus a DEBUG traceback, then takes the exact same
  fail/backoff path as the `ConnectionError` case.
- **`_handle_device_frame` is wrapped in try/except**, mirroring
  `_apply_polled_write`/`_drain_one_pending_stream_refresh`'s existing
  containment: a defect in this NEW speculative path must never be able to
  discard a result the OLD reliable motion path already produced, or tear
  the event socket down over an unrelated bug. **Guarded once per
  `(model_key, exception type name)` per plugin run (F8)** — ERROR on the
  first occurrence, DEBUG for repeats — so a persistent defect on chatty
  NVR (or any other class's) update frames can't flood the Event Log with
  one ERROR per frame.
- `_close_socket` now also closes+`None`s `self.device_socket` — both
  sockets are opened together in `_open_socket` (device socket last, after
  every existing camera/sensor/light/chime/nvr setup), so they are closed
  together too.

New tests: `test_device_router.py` (route/merge_update in isolation, the
real-shaped `ws_devices_capture.json` capture replayed end-to-end, and
`ignored_model_counts`'s `MAX_IGNORED_MODEL_KEYS` overflow bucketing,
mirroring `event_tracker`'s own cap test) and a `test_plugin.py` section
covering: containment of a fatal `_apply_camera_state` with ERROR-once/
DEBUG-repeat (F8); a full `_pump` tick wiring the real capture's
`ledSettings` update onto an Indigo device, alongside an ignored modelKey
and a malformed envelope firing the F4 WARNING once; the retry gate
refusing to construct a socket early (fatal-collaborator constructor) and
succeeding once the gate passes; a device-socket ping failure and a
staleness timeout each tearing down via `_fail_device_socket` without
touching the events socket/`connected`; an NVR `add` actually moving the
registry off the `"nvr"` placeholder via `_rekey_nvr` (making the
pre-existing NVR-add claim true); a full flap scenario (repeated
connect-then-die cycles logging exactly one WARNING with growing backoff,
then a forgiven recovery after `STABLE_AFTER`, then a fresh outage warning
again); a parametrized update-dispatch test pinning the per-class table for
sensor/light/chime; the F1 camera fallback poll (refreshes when the device
socket is down, and is fatal-collaborator-proven to never touch
`get_cameras` when it's up); camera and NVR remove→add→remove
episode-clearing (F2/F3); and F5's broad-exception containment at both the
connect and mid-pump read sites, driven with a real `AssertionError`.

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
| `streamUrlHigh` | String | RTSPS URL, high quality; `""` only when opted out or nothing has ever been stored -- a failed refresh otherwise keeps the prior value |
| `streamUrlMedium` | String | RTSPS URL, medium quality; same rules |
| `streamUrlLow` | String | RTSPS URL, low quality; same rules |
| `streamUrlPackage` | String | RTSPS URL, package-camera stream; stays `""` unless `hasPackageCamera`, same keep-prior rule otherwise |

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

The four stream-URL states above (issue #7) are opt-in per device via the
`exposeStreamUrls` checkbox (default off) -- the URL embeds an access
token, so it is treated as a credential, not plain data.

**Threading/socket safety.** `_rest` sleeps `>= MIN_REST_INTERVAL` (3s) per
call, so nothing that runs on Indigo's main thread, or ahead of `_pump()`,
may perform REST for this feature -- N opted-in cameras would otherwise
block plugin startup, or delay `_pump`'s time-to-first-frame on every
reconnect, by `>= 3N` seconds. The feature is split into a cheap half and a
REST half accordingly:

- `_prime_stream_urls(dev)` -- cheap, REST-free. Called from
  `deviceStartComm` and, for every mapped+enabled device, from
  `_open_socket`. Off: clears the four states immediately (see below). On:
  adds `dev.id` to `self._stream_refresh_pending`, a set, and returns —
  no fetch.
- `_drain_one_pending_stream_refresh()` -- called once per `_pump()` tick,
  after the message-handling block. Pops **at most one** pending device id
  (skipping it if disabled or no longer mapped) and calls
  `_refresh_stream_urls(dev)`, which is itself `_rest`-throttled — so N
  pending devices drain at one per `~MIN_REST_INTERVAL` without ever
  gating socket readiness. Wrapped in a broad `except Exception`: an
  AssertionError from the leak guard, or an Indigo write error, must never
  reach `runConcurrentThread`'s generic handler, which would tear the
  whole event socket down and reconnect-loop forever over what is, at
  worst, one broken camera's stream URLs. Logs
  `f"{dev.name}: stream URL refresh failed (...) - untick 'Expose RTSPS
  stream URLs' on this device if it persists."` and moves on.
- `_refresh_stream_urls_sync(dev)` -- the user-initiated path
  (`refreshStreamUrls` action, and `actionControlUniversal`'s
  `RequestStatus` handler). Synchronous, since a human is waiting and
  REST-blocking one action callback is expected/acceptable. Never silent:
  opted-out logs INFO telling the user to tick the checkbox (and still
  clears the four states); `self.api is None` logs the standard "UniFi
  Protect is not configured" ERROR. Otherwise calls `_refresh_stream_urls`.
- `_refresh_stream_urls(dev)` -- the shared fetch-and-write core, called
  only from the two entry points above (never from `deviceStartComm` or
  `_open_socket` directly):
  - Off: unconditionally writes all four states to `""`. This is the one
    place where "off" must actively *clear* a stored value, not just stop
    refreshing it — leaving a stale token in the database after the user
    opted back out would defeat the point of the checkbox.
  - `self.api is None`: silent no-op (the pump-drain caller has no user to
    report to; the sync caller already handled this case itself).
  - Calls `get_rtsps_streams`. If the body has **none** of the four known
    keys at all, that's a shape error — logs ERROR ("unexpected response
    shape … expected high/medium/low/package"), leaves states untouched,
    does not POST. A key that IS present but not a string (and not null)
    is skipped with a WARNING naming it, not trusted as data.
  - Computes `wanted = ["high", "medium", "low"]` plus `"package"` only
    when the cached camera's `hasPackageCamera` is true, then
    `missing = [q for q in wanted if <value for q is null/absent/invalid>]`.
    If `missing` and the camera **is** in `self.camera_info`, POSTs
    `create_rtsps_streams(camera_id, missing)` and merges the response
    over the GET result for just those qualities. If the camera is **not**
    cached (e.g. before the first camera refresh), does **not** guess —
    logs WARNING "camera capabilities not loaded yet - streams will be
    created on the next refresh" and leaves `missing` as-is.
  - **Never blanks a working URL.** For each of the four qualities: if the
    fresh value (after GET+POST) is present, use it; else if the device's
    **current** stored state for that quality is non-empty, keep it (and
    name the quality in a WARNING, `"...kept previous URL for: high"`);
    only when neither exists does the state become `""`.
  - On `ProtectAPIError` from either the GET or the POST, leaves every
    existing state untouched and logs ERROR, described via
    `self._describe_api_error(exc)` (issue #6) rather than a raw `str(exc)`
    dump, same as every other camera-control error line — the outcome note
    depends on whether anything is currently stored: `"the stored URLs may
    now be stale"` if at least one of the four current states is
    non-empty, else `"no URLs are stored yet"` (a URL that was never
    fetched cannot be stale).
  - On success, logs INFO naming only which qualities ended up present
    (e.g. `"Side Path: stream URLs refreshed (high, medium, low)"`) —
    never a URL. **Every** log line this method emits — the shape error,
    the per-key WARNING, the capabilities-not-loaded WARNING, the
    kept-previous WARNING, the success INFO, and the ProtectAPIError
    ERROR — passes through `_assert_no_url_in_message`, which mirrors
    `protect_api._assert_no_secret` and is checked against **both** the
    fresh values and the device's current stored states (the error path
    has no fresh response to check, so the current states are what could
    leak there instead).
  - Split out of `_refresh_stream_urls` into
    `_fill_missing_stream_qualities` (the POST-and-merge step above) and
    `_clean_stream_response` (the per-key shape/type validation) purely
    for pylint's too-many-locals/branches, the same reason
    `_camera_info_states` was split out of `_write_states`.

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

### Web page sync (issue #27)

`_sync_web_page` installs/updates the bundled `pages/cameras.html` into
`Web Assets/static/pages/` — on `startup()` and on every `closedPrefsConfigUi`
save where `managePage` is on (not just an off→on transition: the sync is
order-independent and idempotent, so re-running it on every save is safe and
also gives a failed sync a retry path). It must **never raise out of
startup**: the whole body is one `try/except`, and every failure path is a
WARNING naming the destination path and that the plugin will retry at the
next config save or plugin restart.

- Byte-compares source and destination before writing; identical bytes is a
  DEBUG no-op, never a write.
- Writes atomically (`tmp` + `os.replace`), and on any exception during the
  attempt, guard-removes the `.tmp` file (its own `try/except OSError: pass`)
  so a failed write never leaves a `cameras.html.tmp` orphan in the
  web-served directory.
- An empty bundled source (`source_bytes == b""`) is refused with a WARNING
  rather than installed — a truncated/corrupt bundle must not clobber a
  working installed page.
- When `managePage` is off, no write happens, but a read-only,
  best-effort check (`_warn_if_managed_page_is_stale`) still compares the
  two files and logs **one INFO** if they differ, naming the bundled
  version and pointing at re-ticking the checkbox or hand-editing. Any
  exception in that off-path check is DEBUG only — an opted-out user must
  not get WARNINGs about a file the plugin isn't managing.
- The catch-all WARNING points at the bundled copy inside the plugin
  bundle (`Contents/Resources/pages/cameras.html`), not `pages/cameras.html`
  in the repo — a zip-installed user has no `pages/` directory.

### Actions (issue #6) — the plugin's first write path

Five `Actions.xml` entries, all `deviceFilter="self.protectCamera"`,
`uiPath="DeviceActions"`, dispatched to a same-named `plugin.py` method:

| Action id | Gate (`featureFlags`) | `mode`/value enum | PATCH body |
|---|---|---|---|
| `setStatusLed` | `hasLedStatus` | `mode` ∈ `{on, off, toggle}` | `{"ledSettings": {"isEnabled": <bool>}}` |
| `setOsdOverlay` | none | each of `showName`/`showDate`/`showLogo` ∈ `{unchanged, on, off}`, `overlayLocation` ∈ `{unchanged}` + the six-way enum | `{"osdSettings": {...}}`, built only from the fields set away from `unchanged` (`showName`→`isNameEnabled`, `showDate`→`isDateEnabled`, `showLogo`→`isLogoEnabled`, `overlayLocation`) |
| `setVideoMode` | value ∈ camera's own `videoModes` | (gate doubles as the enum check) | `{"videoMode": <str>}` |
| `setHdrMode` | `hasHdr` | `hdrType` ∈ `{auto, on, off}` | `{"hdrType": <str>}` |
| `setMicVolume` | `hasMic` | int 0–100 | `{"micVolume": <int>}` |

`setStatusLed`'s `mode="toggle"` does **not** invert the cached
`ledSettings.isEnabled` directly — the cache can be days stale (last
changed from the UniFi app, not this plugin), so toggle first re-reads the
camera via `self._rest(self.api.get_camera, camera_id)`, updates
`camera_info` from that read, and inverts the FRESH value. A failed
re-read aborts with an ERROR and sends no PATCH.

**Every enum/int prop is validated in the callback itself, not just
trusted from the dialog** — `mode not in LED_MODES` (etc, per column
above), naming the field and the invalid value in the ERROR log, no
request made. This exists because a scripter can call `executeAction()`
directly with any value: `mode="ON"` previously read as falsy under
`mode == "on"` and silently turned the LED *off* while logging success.

All five share `_resolve_camera(dev, what)` (resolves `cameraId`, confirms
the plugin is configured, returns the cached camera object — refreshing
once via `_refresh_camera_info()` if it isn't cached yet, which now
returns `True`/`False` so `_resolve_camera` can tell "the id isn't in a
fresh read" — the camera is genuinely gone, an ERROR names it and says to
reselect it — from "the read itself failed", whose cause was already
logged separately by `_refresh_camera_info`) and
`_patch_camera(dev, camera_id, body, what, outcome)` (sends the PATCH via
`_rest`, so it obeys `MIN_REST_INTERVAL` like every other call).

**`patch_camera` validates the response IS the camera object, not merely
a dict** — `parsed.get("id") == camera_id and
isinstance(parsed.get("featureFlags"), dict)` — raising
`ProtectAPIError(..., kind="shape")` otherwise. Without this, a 200 like
`{"id": "cam-1"}` (a proxy wrapper, none of the real fields) would have
been accepted and cached, blanking every hardware state and making every
later capability gate lie "camera does not support X".

**`_describe_api_error(exc, entity="camera")` maps `exc.kind` to one line
of actionable Event Log text**, shared by `_patch_camera`'s error log and
`_refresh_camera_info`'s (NOT used by `takeSnapshot`/`discoverCameras`,
whose existing wording predates this). `entity` names the noun in the
`not_found`/`shape` wording -- the default keeps every existing
camera-only caller's wording unchanged; issue #8's sensor/light/chime/NVR
poll, RequestStatus, and action-error paths pass their own class name
(`"sensor"`/`"light"`/`"chime"`/`"nvr"`) so e.g. a sensor 404 says "sensor
not found", not "camera not found" (this was a real gap: the method was
camera-only text applied verbatim to every device class' poll failures
until `entity` was added):

| `exc.kind` | Wording |
|---|---|
| `auth` | "UniFi Protect rejected the API key. Regenerate it in UniFi OS (Settings > Control Plane > Integrations) and update the plugin config" — a 403 specifically appends "or the key lacks permission for this operation" |
| `rate_limited` | "rate limited by the controller" + " - retry in {N}s" when `retry_after` is known |
| `not_found` | "{entity} not found on the controller (removed or re-adopted?) - reselect it in the device settings" |
| `transport` / `server` | "controller unreachable or errored ({exc}) - outcome unknown, states will update on the next refresh" — deliberately does NOT say "refused": the write may well have landed |
| `shape` | "applied, but the response was unusable - refreshing {entity} info" |
| `bad_request` | "refused by the controller: " + the AJV `issues`, or `str(exc)` if there are none |

On `shape`, `transport`, or `server` — the three kinds where the PATCH's
actual outcome on the camera is genuinely unknown — `_patch_camera`
additionally calls `_refresh_camera_info()` so state catches up to
whatever really happened on the controller, rather than waiting for the
plugin's next scheduled refresh. `camera_info` is left untouched by the
failed PATCH response itself in every case; a refresh may still legitimately
replace it with real data.

**A refused/errored PATCH is always an ERROR in the Event Log, described
via the table above; `camera_info` is only ever replaced by the PATCH's
own response when that response passes the shape check.** On success,
`_patch_camera` replaces `self.camera_info[camera_id]` with the PATCH
response and calls `_apply_camera_state(camera_id, force=True)` — itself
wrapped in `try`/`except`, logging `"{what} applied on the camera, but the
Indigo state update failed: {type}: {exc}"` rather than letting a
`dev.updateStatesOnServer` failure escape the action callback, since the
PATCH already landed on the camera even if Indigo's own state write then
blows up. The success log states the outcome, e.g. `"Set Status LED ->
off"`, `"Set Video Mode -> sport"`, `"Set OSD Overlay -> name on, date
off"` — built by each caller, since only it knows what the PATCH meant
(`_describe_osd_outcome` renders the OSD summary in the same field order
the callback checks them).

**Capability gating happens BEFORE any request**, using the cached
`featureFlags` — never assumed, per the issue (one camera on the reference
rig reports `hasSpeaker: false`). `setOsdOverlay` has no `featureFlags`
gate (OSD text/logo/date toggles are universal); `setVideoMode` gates on
the *value* being a member of that camera's own `featureFlags.videoModes`
rather than a fixed flag. A gate failure is an ERROR log naming the camera
and the missing capability, and no request is made — proven in tests with
a fatal-collaborator API stub whose `patch_camera` (and, for the toggle
re-read, `get_camera`) raises if ever called.

`setVideoMode`'s ConfigUI menu is populated by the dynamic list
`getVideoModeList(filter, valuesDict, typeId, targetId)`, which resolves
`targetId` (the Indigo device id Indigo passes for a `deviceFilter`
action) to its `cameraId` and reads `featureFlags.videoModes` from
`self.camera_info`. When the camera isn't cached yet, it falls back to
`VIDEO_MODE_SPEC_ENUM` — Protect's published OpenAPI enum, only
`default`/`sport`/`slowShutter` proven on the wire — with each label
suffixed `" (unverified)"` so the dialog doesn't imply every listed mode
is confirmed to work on the user's hardware; the PATCH is still validated
against the camera's real list in `setVideoMode`, so picking an unverified
mode the camera doesn't actually support is refused there, not silently
sent. The menu field carries `defaultValue=""`, and
`validateActionConfigUi` rejects an empty selection ("Select a video
mode.") — the empty string can never be a legal mode, so it can only mean
nothing was picked.

`validateActionConfigUi(valuesDict, typeId, deviceId)` — note the third
parameter is the Indigo device id, not an action id, per the SDK's own
naming — handles the checks that belong to the dialog rather than the
camera: `setOsdOverlay` rejects the save when every one of `showName` /
`showDate` / `showLogo` / `overlayLocation` is still `unchanged` (nothing
to send); `setMicVolume` rejects a `micVolume` that doesn't parse as an
int 0–100; `setVideoMode` rejects an empty `videoMode`. All three
callbacks re-check defensively (an empty OSD body, an out-of-range mic
volume, the enum checks described above) because a scripter can call
`executeAction()` directly and bypass the dialog — and its validation —
entirely.

### Issue #19/#21/#25 actions

Five more `Actions.xml` entries, following the #6 shape above but NOT
going through `_resolve_camera`/`_patch_camera` — none of them have a
camera object to gate capability on or refresh state from afterwards
(the API has no PTZ capability flag and no position readback at all, and
the webhook/delete-stream endpoints aren't camera-state writes in the
`_camera_info_states` sense). The precheck for the device-scoped ones is
the cheaper `setChimeVolume` shape instead: `cameraId` present in
`pluginProps`, `self.api` configured — no cached-object lookup.

| Action id | deviceFilter | ConfigUI | PATCH/method |
|---|---|---|---|
| `ptzGotoPreset` | `self.protectCamera` | `slot` menu, "0".."9" (labelled Preset 1-10) | `ptz_goto(camera_id, int(slot))` |
| `ptzPatrolStart` | `self.protectCamera` | `slot` menu, "0".."4" (labelled Patrol 1-5) | `ptz_patrol_start(camera_id, int(slot))` |
| `ptzPatrolStop` | `self.protectCamera` | none | `ptz_patrol_stop(camera_id)` |
| `triggerAlarmWebhook` | none (plugin-level, like `refreshCameras`) | `webhookId` textfield | `send_alarm_webhook(self.substitute(webhookId))` |
| `deleteStreamUrls` | `self.protectCamera` | four checkboxes: `high`/`medium`/`low`/`package` | `delete_rtsps_stream(camera_id, quality)` per ticked quality |

**PTZ slot ranges are NOT symmetric, and this is deliberate, not a typo.**
The OpenAPI spec's own prose says "slot 0-4" for both `/ptz/goto/{slot}`
and `/ptz/patrol/start/{slot}`, but its own `examples` for the goto
endpoint list values up to 9 (`["-1","0","2","8","9"]`), contradicting its
own prose — while `activePatrolSlotString` (the patrol enum) genuinely is
a 5-value 0-4 enum with no such contradiction. `protect_api.py` defines
`PTZ_PRESET_SLOT_MAX = 9` and `PTZ_PATROL_SLOT_MAX = 4`, enforced in
`ptz_goto`/`ptz_patrol_start` respectively; `plugin.py` derives
`PTZ_PRESET_SLOTS`/`PTZ_PATROL_SLOTS` (strings) from those two constants
for its own `Actions.xml` menu and validation, rather than hand-copying a
second pair of ranges. `ptzGotoPreset`/`ptzPatrolStart`'s `slot` is
validated in both `validateActionConfigUi` (against the matching tuple,
via a `typeId -> (slots, message)` lookup) and the callback itself — the
same double-check as every #6 action, because a scripter can call
`executeAction()` with any value, including a real `int` rather than the
dialog's string (`action.props.get("slot")` is normalised to `str(slot)`
first, excluding `bool` — `isinstance(True, int)` is True in Python and
would otherwise launder `True` into slot `"1"`). All three PTZ callbacks
and `deleteStreamUrls` additionally catch `ValueError` around their
`protect_api` call, belt-and-braces against `RTSPS_DELETE_QUALITIES`/
`PTZ_*_SLOTS` ever drifting from what `protect_api.py` itself enforces —
logged as one ERROR, never allowed to escape the callback uncaught.

`triggerAlarmWebhook` validates in two passes, because the real
`self.substitute()` splices in `""` for a dangling variable/device
reference rather than raising — so checking the raw `%%...%%` text isn't
enough, and checking only the post-substitution text can't tell "resolved
to empty" from "never existed". Both `validateActionConfigUi` and the
callback itself call `self.substitute(raw, validateOnly=True)` first (an
`(isValid, errStr)` pair, per the Indigo SDK) and reject/abort on a
dangling reference before ever substituting for real; only then does the
callback call `self.substitute(raw)` (wrapped in `try`/`except`, belt-and-
braces) and apply the existing empty-after-substitution check. A
non-string `webhookId` (e.g. `123` from a scripter) is rejected before
either substitute call. The success log names the resolved id:
`"Trigger Alarm Manager Webhook -> sent (trigger ID 'trig-1')"`.

**A 404 on any of these four is NOT described via `_describe_api_error`'s
`not_found` wording.** That wording says "reselect it in the device
settings", which assumes a camera was deselected — wrong for a PTZ 404
(more likely: non-PTZ camera, unconfigured slot, or missing firmware
support) and nonsensical for the plugin-level webhook (there is no device
to reselect at all). All four share `_describe_not_found_for_endpoint(exc,
hint)`, which renders `f"controller returned 404 ({detail}) - {hint}"`
with an endpoint-specific `hint`. Every other `ProtectAPIError` kind still
goes through `_describe_api_error` unchanged. Separately,
`_describe_api_error`'s own generic fallthrough (an unclassified `kind`,
e.g. `"http"`) now appends any AJV/`error`-derived `exc.issues` in
parentheses, the same detail `bad_request` already surfaces.

`deleteStreamUrls` loops over the ticked qualities (`_truthy`-checked,
same string-`"false"` handling as `exposeStreamUrls`) and calls
`delete_rtsps_stream` once per quality via `_delete_one_stream_quality`,
independently — one failing quality must never skip the others. Outcomes
are tracked in three buckets, and the closing INFO line names all three
that are non-empty, e.g. `"deleted: high; already gone: package; failed:
low"`:

- **`not_found`** is genuinely ambiguous — it could mean the STREAM was
  already gone (fine, that's what the action wants) or the CAMERA itself
  is gone (a much bigger deal). On the first `not_found` in a call, the
  camera's presence is checked once (`camera_id in self.camera_info`,
  refreshing via `_refresh_camera_info()` if not already cached) and
  cached in a `[checked, present]` pair shared across the remaining
  qualities in the same call — a genuinely-failed refresh is treated the
  same as "still absent" (there is no third, better answer). If the
  camera is gone (or its status is unknown), the WHOLE action aborts
  immediately: no state is cleared, no summary is logged, no recreate
  warning fires, and the remaining qualities are never even attempted
  (proven in tests with a fatal-collaborator API). If the camera IS
  present, the quality is logged as `already_gone` (one INFO line naming
  it) and its state is cleared exactly like a real delete.
- **`transport`/`server`** does NOT reuse `_describe_api_error`'s generic
  "states will update on the next refresh" wording — that is false here:
  with `exposeStreamUrls` off nothing refreshes on its own at all, and
  with it on, only the NEXT explicit refresh recreates a quality, not
  passive settling. The dedicated wording says the outcome is unknown and
  names the two ways to re-sync (re-run this action, or Refresh Stream
  URLs with expose ticked).
- Any other kind logs one ERROR via `_describe_api_error` naming the
  quality, exactly as before.

Each quality's state clear (`_clear_one_stream_state`) is wrapped exactly
like `_patch_camera`'s own post-write guard: the controller-side delete
already happened, so a `dev.updateStatesOnServer` failure is logged as
"deleted on the controller, but the Indigo state update failed" and still
counts as deleted — it must never look like the delete itself failed, and
must never abort the remaining qualities. Every log line in this action
(all outcomes, plus the closing summary and recreate warning) passes
through `_assert_no_url_in_message` against the device's current stored
stream values, mirroring the rest of the issue #7 stream-URL family.

**Streams are never deleted automatically anywhere else in this plugin —
`deleteStreamUrls` is the only path that removes one.** This is
deliberate: `_refresh_stream_urls` only ever *creates* missing qualities
(via `create_rtsps_streams`) or falls back to whatever was already
stored, it never deletes on its own initiative, because a momentary GET
returning null for a quality that used to work must not be read as "the
user wants this gone." The consequence, stated in both the action's help
label and its success path: `_warn_if_deleted_streams_will_be_recreated`
computes the recreatable subset EXACTLY as `_refresh_stream_urls` does
(`high`/`medium`/`low` always, `package` only when the cached camera
reports `hasPackageCamera`) and warns only when the intersection of
`deleted + already_gone` with that subset is non-empty, naming those
qualities specifically — deleting `package` on a camera without a
package lens must not claim it'll come back, because it never will. The
warning also only fires while `exposeStreamUrls` is still on (a refresh
with it off only clears state, per `_refresh_stream_urls`, never
recreates). The trigger list is a device restart, an event-socket
reconnect, **Send Status Request** (routes to `_refresh_stream_urls_sync`
via `actionControlUniversal`), or the Refresh Stream URLs action —
confirmed against `_refresh_stream_urls`'s actual missing-quality/create
logic, not guessed. A delete that's meant to be permanent needs
`exposeStreamUrls` unticked too.

**Single source of truth for the RTSPS quality tuple**: `protect_api.py`
defines `RTSPS_QUALITIES = ("high","medium","low","package")`, used by
`delete_rtsps_stream`'s own validation; `plugin.py` imports it as
`RTSPS_DELETE_QUALITIES` for its ConfigUI checkboxes and
`validateActionConfigUi`, rather than hand-copying the tuple a third time.

All five are SPEC-DERIVED, UNVERIFIED against the reference rig — no PTZ
camera, no Alarm Manager alarms configured there.

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

Issue #7 (stream URLs) added, in `test_plugin.py` and `test_protect_api.py`:

- opted-out (absent, `False`, `"false"`, `"False"`) writes all four states
  `""` and never touches a fatal-collaborator API; opting out also
  **clears** previously-populated states, not just leaves them alone
- opted-in (`True`, `"true"`, `"True"`) writes from the GET response, a
  null `package` becomes `""`, and neither the URL/token nor an
  exception's leaked body ever appears in ANY log record on the success
  path OR the error path (both also assert the expected INFO/ERROR record
  actually exists, so the token-absence check can't pass on a silent
  no-op)
- a partial-null GET with a prior value: kept and named in a WARNING when
  the camera is not cached (no guessed POST); replaced when the camera IS
  cached and POST is issued for just the missing qualities
- `{}` and `{"error": "x"}` bodies are shape errors (ERROR, states
  untouched, no POST); a non-string value for a present key is skipped
  with a WARNING naming it
- error wording: "the stored URLs may now be stale" when something is
  currently stored, "no URLs are stored yet" when nothing is
- `deviceStartComm` only ever QUEUES an opted-in device
  (`_stream_refresh_pending`) and never fetches — proven with a
  fatal-collaborator API, regardless of whether `self.api` is even
  configured; `_open_socket` does the same for every mapped+enabled device
  and must never touch a `get_rtsps_streams`/`create_rtsps_streams` that
  raises
- the pump drain: two pending devices drain one per `_pump()` tick without
  the socket disconnecting; a device whose `updateStatesOnServer` raises
  is caught (fatal-collaborator form) and logs the "untick" ERROR rather
  than escaping to `runConcurrentThread`'s generic handler; monkeypatching
  `_assert_no_url_in_message` itself to raise is likewise caught and
  logged, not propagated
- the synchronous action (`refreshStreamUrls`) and `RequestStatus` paths,
  driven through the real Indigo callbacks: opted-out logs INFO and still
  clears the four states; unconfigured (`self.api is None`) logs the
  standard "not configured" ERROR
- `protect_api.get_rtsps_streams`/`create_rtsps_streams`: every
  ProtectAPIError's `.body` is always `""` (HTTP error, transport failure,
  or shape mismatch), tested the same way for both methods, including a
  body deliberately constructed to contain a token
---

## Issue #8: sensors, lights, chimes, NVR

> **UNVERIFIED -- spec-derived.** Everything in this section (except the
> NVR's `armMode` fields, which were captured live) is built from Protect's
> published OpenAPI 3.1 spec (v6.2.83), not from a real device. The
> reference rig's `GET /sensors`, `GET /lights`, and `GET /chimes` all
> return `[]` -- there is nothing to capture against. Do not "fix" this
> code to match a hunch about real hardware behavior without a debug
> capture backing it; file a bug with one instead.

### `event_tracker.py`: generic lifecycle families + pulses

Four new **lifecycle** families, each routed by `item.type` exactly like
MOTION/AUDIO (`add`/`update` keepalive/`update`-with-`end`, traps 1 and 2
apply identically):

| `item.type` | Internal family (`FAMILY_*` constant) | Per-event type source |
|---|---|---|
| `sensorMotion` | `FAMILY_SENSOR_MOTION` ("sensorMotion") | none (always empty) |
| `sensorWaterLeak` | `FAMILY_SENSOR_LEAK` ("sensorLeak") | none |
| `sensorAlarm` | `FAMILY_SENSOR_ALARM` ("sensorAlarm") | `metadata.alarmType.text` |
| `sensorTamper` | `FAMILY_SENSOR_TAMPER` ("sensorTamper") | none |

Reached via the generic methods, not per-family wrappers: `family_active(family,
device_id)`, `family_types(family, device_id)`, `last_family_ms(family,
device_id)`, `clear_family(family, device_id)`. `is_active`/`detect_types`/
`audio_active`/etc. are unchanged and are NOT reimplemented on top of the
generic methods -- they predate this generalization and share the same
underlying per-family storage, so the two APIs can never disagree.

Six **pulse** types -- fire-once notifications, never an active/idle span
(the OpenAPI spec gives each a nullable `end`, but the type's own
description is a point-in-time notice, e.g. "has entered an open state",
not a lifecycle): `sensorOpened`, `sensorClosed`, `sensorBatteryLow`,
`sensorExtremeValues`, `sensorSmokeTest`, `lightMotion`. Any `add`/`update`
frame of a pulse type is recorded via `last_pulse(device_id, pulse_type) ->
{"start": epoch_ms|None, "metadata": dict}` and reports the device
changed; a repeat of the same event id is a no-op, deduped against the
**same** finished-id cache lifecycle events use (event ids are globally
unique across the whole schema, so sharing is safe). Pulses **survive**
`clear_family`/`clear_camera` (they are a history, not live state) and are
**dropped** by `reset()`.

`KNOWN_UNSUPPORTED_EVENT_TYPES` now holds only `"ring"` -- every other
previously-unsupported type is a recognized family or pulse.

### `protect_api.py`: new methods

```python
def _request(self, method: str, path: str, params=None, body=None) -> Any: ...
    # General GET/PATCH (or any verb) helper, added because this branch's
    # base did not yet have one. Issue #6 (stacked earlier, rebased in
    # later) is expected to add a method with this exact signature -- on
    # rebase, prefer #6's and drop this one if they are equivalent.

def get_sensors(self) -> list[dict]: ...
def get_sensor(self, sensor_id: str) -> dict: ...
def patch_sensor(self, sensor_id: str, body: dict) -> dict: ...

def get_lights(self) -> list[dict]: ...
def get_light(self, light_id: str) -> dict: ...
def patch_light(self, light_id: str, body: dict) -> dict: ...

def get_chimes(self) -> list[dict]: ...
def get_chime(self, chime_id: str) -> dict: ...
def patch_chime(self, chime_id: str, body: dict) -> dict: ...

def get_nvr(self) -> dict: ...
    # GET /nvrs. Live-verified 2026-08-31 on 7.2.105: returns a SINGLE
    # OBJECT, not an array -- matching the OpenAPI spec's response schema
    # for this path (`nvr`, not `array<nvr>`) despite the plural path name.
    # Tolerates a one-element list defensively; raises on [], a
    # multi-element list, or any other non-dict/non-list shape.
```

Every method follows the same shape-validation and error style as
`get_cameras`/`get_camera`.

**`ProtectAPIError.kind` now accepts an explicit override** (`kind=` on the
constructor) in addition to the existing status-derived classification --
a response that parses as JSON but is the wrong shape (an empty list, a
multi-element list, a non-dict) is not really an HTTP problem, so
`_expect_dict`/`_expect_list_of_dicts` and `get_nvr`'s one-element-list
check all raise with `kind="shape"` explicitly rather than the misleading
`"transport"` a `status=None` would otherwise imply. `plugin.py`'s
poll-failure guard (below) keys on this.

### Live NVR facts (verified 2026-08-31, UNVR 7.2.105)

`GET /nvrs` returns:

```json
{"id": "...", "modelKey": "nvr", "name": "...", "type": "UNVRINSTANT",
 "guid": "...", "mac": "...",
 "doorbellSettings": {...},
 "armMode": {"status": "disabled", "armedAt": null, "willBeArmedAt": null,
             "breachDetectedAt": null, "breachEventCount": 0,
             "breachTriggerEventId": null, "breachEventId": null}}
```

`armMode`, `type`, `guid`, and `mac` are **not** in the OpenAPI spec --
observed-only. `armMode` may be absent entirely on some Protect versions;
tolerate both a dict and its absence.

### `plugin.py`: device classes

Registries mirror `self.cameras`/`self.camera_info`: `self.sensors`,
`self.sensor_info`, `self.lights`, `self.light_info`, `self.chimes`,
`self.chime_info`, and `self.nvrs`/`self.nvr_info` (`self.nvrs` is keyed by
the NVR's own id once a poll has learned it, or the literal string `"nvr"`
before that -- `_rekey_nvr()` moves the device-id set across on first
successful poll).

**A poll bug must never be able to kill the motion socket.** Every
per-device write for these four classes goes through
`_apply_polled_write(dev, write_fn, *args)`, which wraps the call in
`try/except Exception`, logs `ERROR "{dev.name}: could not apply polled
state (...) - the event socket is unaffected"` + a DEBUG traceback, and
continues to the next device. `_apply_sensor_state`/`_apply_light_state`/
`_apply_chime_state`/`_apply_nvr_state` all route through it, which also
covers `_mark_all_disconnected` (it calls these same methods) without any
separate wrapping there. Each class's poll FETCH is *also* wrapped in a
broad `except Exception`, not just `ProtectAPIError` -- a non-ProtectAPIError
bug reaching `_poll_devices`/`_pump`/`runConcurrentThread` would be
mistaken for an event-socket fault and tear the camera connection down for
a completely unrelated reason. This is not theoretical: `(info or
{}).get("batteryStatus", {}).get("percentage")` does **not** guard a JSON
`batteryStatus: null` -- the key is *present*, so the `{}` default never
applies, and `.get("percentage")` on `None` raised `AttributeError` right
out of `_write_sensor_states`, into `_open_socket`/`runConcurrentThread`'s
generic handler, logged as a socket error, and reconnected forever. The
`_as_dict(value)` helper (`value if isinstance(value, dict) else {}`)
fixes every such nested-object read across sensor/light/chime/nvr, and
also guards a wrong-type value (e.g. a list), not just null.

**Polling** (`_poll_devices()`, `DEVICE_POLL_INTERVAL = 60.0`): called from
`_pump()` when due, and from `_open_socket()` right after
`_refresh_camera_info()` -- but ONLY when at least one non-camera device is
registered, so an API that raises if touched is never touched with only
cameras present. Each registered class costs one throttled REST call
(`_rest` enforces `MIN_REST_INTERVAL = 3.0s`), so a full poll cycle inside
`_pump()` can block that loop for up to *N* × 3s; any WS frames that
arrive meanwhile simply queue in the socket's own read buffer -- an
accepted trade against a second thread. Each poll's own wall-clock anchor
(`now_ms`) is captured **before** the REST request goes out, not after it
returns -- a live pulse arriving mid-request must never be mistaken for
older than a poll that, from the pulse's point of view, hasn't finished
yet -- and `_device_last_poll_ms[protect_id]` is advanced only **after**
that device's write is attempted, never before.

A poll failure (`ProtectAPIError` OR any other `Exception` from the fetch)
does not merely skip the write: `_mark_class_unavailable(registry,
state_key)` PROACTIVELY writes `{state_key: "unavailable", connected:
<current>}` to every registered device of that class -- nothing else,
`lastPoll` untouched, poll-derived fields left at whatever they last were.
A stale `"CONNECTED"` sitting there through an outage with no signal
anything is wrong is exactly the failure mode this exists to prevent.
The failure guard is keyed on `(class_name, exc.kind)` (falling back to
`type(exc).__name__` for a non-`ProtectAPIError`) via
`_report_poll_failure`/`_clear_poll_failure`/`_describe_api_error` -- ERROR
once per (class, kind) per outage, DEBUG for repeats, and INFO once on
recovery (`"{class} polling recovered"`). A successful single-device
`RequestStatus` also clears the guard for that class.

**A registered id absent from a successful list poll** (the reference
rig's actual behaviour: `GET /sensors` returns `[]`) is handled the same
way as a device the list poll never mentions at all: `_warn_absent_from_list`
logs `WARNING` once per `(class, protect_id)` absence-episode ("removed
from Protect, or the API returned an empty list; keeping last-known
values"), `self.sensor_info`/etc. simply has no entry for that id (the
cache is rebuilt wholesale from each successful list, not merged), and the
resulting `_write_*_states` call naturally takes the same "no info" path
described below -- `*State=unavailable` + `connected`, `lastPoll`
untouched, `_device_last_poll_ms` not advanced. Cleared
(`_clear_absent_from_list`) the moment the id reappears in a later list.

**Never a fabricated value before the first poll.** Every poll-derived
state key (`isOpen`, `batteryLow`, `mountType`, `temperature`/`humidity`/
`lightLevel`, `lastOpenChange`, native `batteryLevel`; a light's
`isLightOn`/`isDark`/`forceEnabled`/`lightMode`/`ledLevel`; a chime's
`pairedCameraCount`/`ringVolume`; an NVR's `nvrName`/`nvrModel`/
`protectVersion`/`armedAt`/`breachDetectedAt`/`breachEventCount`) is
gated on `if info:` in its `_write_*_states` and simply **omitted** from
the batch -- not written as `False`/`0`/`""` -- whenever there is no cache
yet (brand-new device before the first poll, a REST failure that never
populated the cache, or an id absent from a list). This is what fixed
"every restart fires 'Floodlight turned off'": `onOffState` for a light IS
`isLightOn`, purely poll-derived with no lifecycle fallback at all, so it
used to be written `False` unconditionally the instant `deviceStartComm`
ran, before any poll had ever told the plugin the light's real state.
Lifecycle booleans (`motionDetected`/`leakDetected`/`alarmTriggered`+
`alarmType`/`tampered`, and light's `pirMotionDetected`) are the one
exception -- they come from the tracker, not the poll cache, so `False` is
genuine information ("no event has happened yet") even with zero polls,
and are always written. `sensorState`/`lightState`/`chimeState`/
`armStatus` (`_state_or_unavailable`/inline equivalent) and `connected`
are likewise always written, `STATE_UNAVAILABLE` covering the no-info
case -- and, since `.get(key, default)` only substitutes when the key is
*absent*, not when it is present-but-`null`, these use `info.get(key) or
STATE_UNAVAILABLE`, never the two-arg form, so a JSON `null` can never
land in what Indigo declares as a String state.

**Reconciliation** (poll is authoritative for measurements/flags; between
polls, the live event stream drives the boolean):

- Sensor `motionDetected`: driven live by `family_active(FAMILY_SENSOR_MOTION,
  ...)`. At poll time, if the poll's own `isMotionDetected` is `False`
  while the tracker still thinks it's active, the tracker is corrected
  (`clear_family`, DEBUG-logged) -- never the other direction. The reverse
  disagreement (poll says active, tracker idle -- a lost `add` frame would
  look like this) is DEBUG-logged but NOT corrected; the tracker is
  trusted.
- Sensor `isOpen`: the poll's `isOpened`, overridden by whichever of a
  `sensorOpened`/`sensorClosed` pulse has a newer `start` than the poll's
  own `openStatusChangedAt` (and than each other, if both exist) -- or,
  when `openStatusChangedAt` is `null` (both entries in
  `tests/fixtures/sensors_spec.json` have it null, so this is not an edge
  case), versus `_device_last_poll_ms[sensor_id]` instead. Without that
  fallback, a `null` `openStatusChangedAt` compares as "always older" than
  any real timestamp, so a single stale pulse would win FOREVER instead of
  self-expiring at the next poll like every other override in this
  module. The poll-baseline fallback is a COMPARISON ANCHOR ONLY and is
  never itself reported: `lastOpenChange` is written only when the
  winning timestamp is a REAL one -- `openStatusChangedAt` itself, or an
  actual `sensorOpened`/`sensorClosed` pulse -- and is simply omitted
  (not written as `""`, not re-written) otherwise. An earlier version
  reported the poll-baseline fallback as `lastOpenChange` directly, which
  made it advance by one poll interval every cycle forever even when
  nothing about the sensor had changed. Every pulse that loses this
  comparison is DEBUG-logged with both timestamps.
- Sensor `batteryLow`: poll's `batteryStatus.isLow`, OR a `sensorBatteryLow`
  pulse newer than `_device_last_poll_ms[sensor_id]` -- self-expiring,
  since the next poll always advances that timestamp. A losing pulse is
  DEBUG-logged the same way.
- Sensor `temperature`/`humidity`/`lightLevel`: poll's `stats.<metric>.value`,
  immediately overridden by a `sensorExtremeValues` pulse for the matching
  metric (`metadata.sensorType.text` -- `"light"` maps to the `lightLevel`
  state) newer than `_device_last_poll_ms` -- same self-expiring pattern,
  same DEBUG log on a loss.
- Light `pirMotionDetected`: treated like a sensor lifecycle boolean (see
  disconnect rule below) -- forced `False` when the socket is down.
  Otherwise, the poll's `isPirMotionDetected`, overridden `True` by a
  `lightMotion` pulse newer than `_device_last_poll_ms[light_id]` (a
  losing pulse DEBUG-logged).
- Light `lastMotion`: the newer of the poll's own `lastMotion` field and a
  `lightMotion` pulse's `start`.

**Disconnect rule, extended**: on socket loss, sensor
`motionDetected`/`leakDetected`/`alarmTriggered`+`alarmType`/`tampered`
and light `pirMotionDetected` go `False` (`alarmType` goes `""`) with
`connected` `False` -- the same honesty rule as camera motion, because
these are the fields driven by the live event stream. Everything else on
sensors/lights (`isOpen`, `batteryLow`, `temperature`, `isLightOn`, ...)
and everything on chimes/NVR is REST-poll-derived and is **kept** at its
last-known value, exactly like a camera's `cameraModel`/`videoMode`
survive a socket loss. `connected` on every new device type reflects the
same event-socket health signal as a camera's `connected`
(`_is_connected()`), even for chimes/NVR, which have no live feed of their
own -- there is one connectivity concept in this plugin, not a per-class
one. `_open_socket()` re-applies `connected=True` explicitly for every
registered sensor/light/chime/NVR device right after the handshake,
regardless of whether the poll that follows succeeds -- without this, a
failing first poll after reconnect could leave these devices reporting
`connected=False` (stuck from the prior disconnect) even though the event
socket, which is what `connected` actually means here, is genuinely back
up.

**Recovery limitation (documented, not fixed -- there is no fix available
from this API):** after a plugin restart, `leakDetected`/`alarmTriggered`+
`alarmType`/`tampered` read `False` until a NEW live event arrives, even
if the sensor's last-reported condition was still active when the plugin
went down. The poll object carries only a `*DetectedAt`/`alarmTriggeredAt`
timestamp for these three, never a "currently active" flag the way
`isOpened`/`isMotionDetected` exist for open/motion -- so there is no
honest way to reconstruct "is it STILL leaking/alarming/tampered right
now" from a poll alone, and the plugin does not guess. What it does
instead: `lastLeak` (newest of `leakDetectedAt`/`externalLeakDetectedAt`),
`lastAlarm` (`alarmTriggeredAt`), and `lastTamper` (`tamperingDetectedAt`)
surface those raw poll timestamps as their own String states (ISO or
omitted when the poll never reported one) so the information is at least
visible, even while the boolean itself is silent. Sensor `isOpen` and
light `pirMotionDetected` do NOT have this gap -- the poll gives a real
current-state field for both (`isOpened`, `isPirMotionDetected`), so no
information is lost across a restart there.

**`primaryState` resolution** (`protectSensor` only): `"auto"` (default)
maps mount type to a boolean via `SENSOR_MOUNT_PRIMARY_STATE` --
`door`/`window`/`garage` -> `isOpen`, `leak` -> `leakDetected`, `none` (or
anything unrecognized) -> `motionDetected`. Any other explicit choice
(`open`/`motion`/`leak`/`alarm`) wins outright. `onOffState` itself follows
the same "never fabricated" rule as every other poll-derived key: if the
resolved primary is `"open"` and there is no poll cache yet, `onOffState`
is omitted entirely rather than written `False` -- `"motion"`/`"leak"`/
`"alarm"` are lifecycle booleans and are always safe to write.

**`actionControlDevice`** (protectLight only -- the only new device type
that declares TurnOn/TurnOff/Toggle): the `PATCH .../lights/{id}` (body
`{"isLightForceEnabled": <bool>}`) and the re-`GET` are two SEPARATE
`try`/`except` blocks, not one. A PATCH failure logs `"could not set
light"`; a GET failure AFTER a successful PATCH logs a different message
-- `"force flag set, but could not re-read the light (...) - states may be
stale until the next poll"` -- because the light genuinely did change,
only the re-read failed, and merges the (possibly partial) PATCH response
into the cache immediately so that fact isn't lost even if the GET never
lands. Toggle, if nothing is cached yet (e.g. right after startup), GETs
the light first rather than guessing `isLightForceEnabled` is `False`.
"Off" only clears the force flag -- the floodlight's own motion mode, if
any, can still turn it on.

**`setLightLevel`/`setChimeVolume`** merge `{**cached, **response}` from
the PATCH into the existing cache rather than replacing it wholesale --
some PATCH endpoints return only the changed subset, and replacing the
cache with a partial object would silently drop every other previously-
known field. A `None`/empty PATCH response is still treated as a success
(not an error) and triggers a re-`GET` for an authoritative view instead
of guessing the new shape; a failure on that re-`GET` logs "...set, but
could not re-read... - states may be stale until the next poll" the same
way the light-action path does.

`setChimeVolume` specifically: on a cache miss (nothing in `chime_info`
yet -- before the first poll, or after a poll failure), it `GET`s the
chime FIRST, mirroring the light Toggle path's same rule -- "no
ringSettings" is only ever reported after an actual read, never guessed
from an empty cache. The PATCH body sends only the four documented
`ringSettings` keys (`cameraId`, `repeatTimes`, `ringtoneId`, `volume`)
per entry, dropping anything else a cached entry might carry: the PATCH
item schema is `additionalProperties: false` while the GET schema is not,
so echoing a cached entry wholesale can be rejected by the controller.

**`actionControlUniversal` RequestStatus**, every new type: an immediate
single-device `GET` (not the batch `GET .../sensors` etc.) + state write,
mirroring the camera path's `_refresh_camera_info()` + `_apply_camera_state`,
and clears that class's poll-failure guard on success.

### State tables

**protectSensor** (`type="sensor"`, no subType -- one device tracks up to
five different physical sensor kinds):

| State | Type | Source |
|---|---|---|
| `isOpen` | Boolean | poll `isOpened`, pulse-overridden (see above) |
| `motionDetected` | Boolean | `family_active(FAMILY_SENSOR_MOTION, ...)` |
| `leakDetected` | Boolean | `family_active(FAMILY_SENSOR_LEAK, ...)` |
| `alarmTriggered` | Boolean | `family_active(FAMILY_SENSOR_ALARM, ...)` |
| `alarmType` | String | `family_types(FAMILY_SENSOR_ALARM, ...)`, comma-joined |
| `tampered` | Boolean | `family_active(FAMILY_SENSOR_TAMPER, ...)` |
| `batteryLow` | Boolean | poll `batteryStatus.isLow`, pulse-overridden |
| `temperature`/`humidity`/`lightLevel` | Number | poll `stats.*.value`, pulse-overridden; omitted (not `0`) when unknown |
| `mountType` | String | poll `mountType` |
| `sensorState` | String | poll `state`, or `STATE_UNAVAILABLE` |
| `connected` | Boolean | event socket health |
| `lastMotion` | String | ISO or `""`; `_sensor_last_motion_ms` merges the tracker's `last_family_ms(FAMILY_SENSOR_MOTION, ...)` with the poll's `motionDetectedAt` (newest wins) -- without the poll side, this read `""` after every restart |
| `lastOpenChange` | String | ISO, omitted (not `""`, never re-written) unless the `isOpen` reconciliation's winning timestamp was REAL (`openStatusChangedAt` or an actual pulse) -- the internal poll-baseline fallback must never be reported |
| `lastLeak` | String | ISO, omitted if null; newest of poll `leakDetectedAt`/`externalLeakDetectedAt` -- see "Recovery limitation" above |
| `lastAlarm` | String | ISO, omitted if null; poll `alarmTriggeredAt` -- see "Recovery limitation" above |
| `lastTamper` | String | ISO, omitted if null; poll `tamperingDetectedAt` -- see "Recovery limitation" above |
| `lastPoll` | String | ISO, only present on a poll-triggered write |
| *(native)* `batteryLevel` | -- | poll `batteryStatus.percentage`, via `updateStateOnServer` -- **not** a `<State>`. The OpenAPI spec marks `batteryStatus` "[DEPRECATED] Use wirelessConnectionState.batteryStatus instead", but `wirelessConnectionState` appears nowhere else in the spec -- real firmware may still populate this field, or may already use the undocumented one; a likely first hardware-report item |

**protectLight** (`type="relay"`): `onOffState` = `isLightOn`.

| State | Type | Source |
|---|---|---|
| `isDark` | Boolean | poll `isDark` |
| `pirMotionDetected` | Boolean | poll `isPirMotionDetected`, pulse-overridden; disconnect-forced False |
| `forceEnabled` | Boolean | poll `isLightForceEnabled` |
| `ledLevel` | Integer | poll `lightDeviceSettings.ledLevel`; skipped if unparseable |
| `lightMode` | String | poll `lightModeSettings.mode` |
| `lightState` | String | poll `state`, or `STATE_UNAVAILABLE` |
| `connected` | Boolean | event socket health |
| `lastMotion` | String | ISO or `""` (see reconciliation above) |
| `lastPoll` | String | ISO, poll-triggered writes only |

**protectChime** (`type="custom"`):

| State | Type | Source |
|---|---|---|
| `chimeState` | String | poll `state`, or `STATE_UNAVAILABLE` |
| `pairedCameraCount` | Integer | `len(cameraIds)` |
| `ringVolume` | Integer | first `ringSettings` entry's `volume`; omitted if no entries |
| `connected` | Boolean | event socket health |
| `lastPoll` | String | ISO, poll-triggered writes only |

**protectNvr** (`type="custom"`, no device picker -- one NVR per console):

| State | Type | Source |
|---|---|---|
| `nvrName` | String | poll `name` |
| `nvrModel` | String | poll `type` (observed-only field); omitted if absent |
| `protectVersion` | String | `GET /meta/info` (`applicationVersion`), refreshed alongside each NVR poll |
| `armStatus` | String | `armMode.status`, or `STATE_UNAVAILABLE` if `armMode`/`nvr_info` absent |
| `armedAt` | String | ISO or `""` |
| `breachDetectedAt` | String | ISO or `""` |
| `breachEventCount` | Integer | `armMode.breachEventCount` |
| `connected` | Boolean | event socket health |
| `lastPoll` | String | ISO, poll-triggered writes only |

No actions -- arm/disarm is not in the published integration API.

### Testing

`tests/fixtures/{sensors,lights,chimes,nvrs}_spec.json` (spec-derived, see
`tests/fixtures/README.md`). Per workspace convention, the adversarial
question is "when could this report idle/unavailable/kept and be wrong?":

- fatal-collaborator: `_poll_devices()` with only cameras registered never
  touches any non-camera API method; with exactly one sensor registered,
  only `get_sensors` is touched
- a poll failure keeps every last-known value, marks the class's own state
  string `"unavailable"`, never writes `lastPoll`, and logs ERROR exactly
  once across two consecutive failures
- `primaryState=auto` picks the right boolean per mount type, and an
  explicit choice overrides mount type
- `isOpen` reconciliation both directions: a pulse newer than the poll's
  `openStatusChangedAt` wins; a poll newer than the pulse wins
- a poll's `isMotionDetected: false` clears a tracker family stuck active
  from a lost `end` frame
- `batteryLevel` lands as a native single-key write, never inside the
  batched `updateStatesOnServer` call
- disconnect forces sensor lifecycle booleans and light `pirMotionDetected`
  False while keeping poll-derived values (`temperature`, `onOffState`)
- light TurnOn does PATCH-then-GET in that order; a refused PATCH logs
  ERROR and leaves every state untouched (proven with a `get_light` that
  raises if called)
- `setChimeVolume` on a chime with no `ringSettings` errors without
  calling `patch_chime`
- NVR `armStatus` reads `"unavailable"` both before any poll and when
  `armMode` itself is absent from a present `nvr_info`
- declared-vs-written, extended to iterate every `<Device>` in Devices.xml
  (not just the camera), with `batteryLevel` explicitly exempted from both
  the "undeclared" and "never written" checks since it is a native
  property, never a `<State>`

#### Round-2 review coverage (silent-failure hunter + test analyst)

- fatal-collaborator, driven through `_pump`'s own poll tick (not calling
  `_poll_devices` directly): a sensor device whose `updateStatesOnServer`
  raises never escapes into `_pump`/`runConcurrentThread`, the socket
  stays open, and the light device polled in the same cycle still gets
  its write; a non-`ProtectAPIError` raised by a fake `get_sensors` is
  caught the same way
- `batteryStatus: null` (both the JSON-null case and a wrong-type value)
  never raises, and `batteryLevel` is simply skipped
- a poll failure now WRITES `{class}State="unavailable"` + `connected`
  (rewritten from an earlier version of this test that asserted NO new
  write happened at all, which blessed the actual bug -- a stale
  `"CONNECTED"` surviving an outage with no signal anything was wrong);
  every other poll-derived key and `lastPoll` are proven untouched by
  diffing the exact key set of the failure's write batch
- a registered id absent from a successful list poll: WARNING once,
  `*State=unavailable`+`connected` only, `mountType`/`lastPoll` kept; the
  guard clears the moment the id reappears (proven by polling empty twice,
  then present, then empty again, expecting exactly 2 WARNINGs total)
- before the first poll: a sensor writes only its lifecycle booleans +
  `connected` + `sensorState="unavailable"` (no `isOpen`/`batteryLow`/
  `mountType`/measurements/`batteryLevel`); a light writes no `onOffState`
  and none of `isDark`/`forceEnabled`/`lightMode`/`ledLevel` either; a
  sensor with `primaryState=open` and no cache omits `onOffState` entirely
  (never fabricates `False`)
- `_open_socket` re-applies `connected=True` for a registered sensor with
  `_poll_devices` AND `_refresh_camera_info` both stubbed to no-ops, so
  only the explicit reapply can be responsible for the write
- `isOpen`/`sensorState`/`armStatus` never receive a bare `None` when the
  corresponding poll field is present-but-null
- DEBUG fires with both timestamps when a stale pulse is discarded
  (`sensorOpened` vs a newer poll baseline), and when a poll disagrees
  with an idle tracker (`isMotionDetected: true`, tracker idle)
- `batteryLow` pulse override is proven to clear at the NEXT poll (not
  merely "a poll clears it eventually") using deterministic epoch-ms
  timestamps throughout, since two real `_poll_sensors()` calls back to
  back would make "the next poll is later than the pulse" a race against
  test execution speed
- `sensorExtremeValues` updates `temperature` and (`sensorType.text ==
  "light"`) `lightLevel`
- `isOpen` with both `sensorOpened`/`sensorClosed` pulses present, in each
  handling order, always resolves to whichever has the truly newest
  `start`, and `lastOpenChange` matches that timestamp
- all four `tests/fixtures/*_spec.json` fixtures are loaded and driven
  through their `_write_*_states`, asserting on the fixture's own `state`/
  `armMode.status` field rather than a hardcoded value
- a pulse frame carrying a non-null `end` (the OpenAPI spec's own nullable
  `end` on every pulse type) is still recorded via `last_pulse`, not
  routed through the lifecycle add/finish machinery
- disconnect additionally asserts `alarmTriggered`/`tampered` go `False`
  and `alarmType` clears to `""`
- `primaryState="leak"`/`"motion"` each drive `onOffState` end-to-end, not
  just their own boolean state
- light TurnOn/Off/Toggle: a GET failure after a successful PATCH merges
  the PATCH response and logs the "force flag set, but could not re-read"
  message, never "could not set light"; Toggle with an empty cache GETs
  before guessing; `setLightLevel`/`setChimeVolume` merge a partial PATCH
  response instead of replacing the cache, and treat an empty/`None`
  response as success followed by a re-GET
- two outages with a recovery in between log exactly two ERRORs and one
  INFO ("... polling recovered"); `RequestStatus` for each of the four
  types calls only the single-device GET, with a fatal fake proving the
  list method is never touched
- `batteryLevel`'s declared-vs-written exemption in
  `test_every_written_state_is_declared_and_legal` is scoped to
  `protectSensor` only, so another type accidentally writing it would
  still be caught as undeclared

#### Round-3 review coverage (final code-quality pass, PR #15)

- `lastOpenChange` is never written across three consecutive polls with a
  null `openStatusChangedAt` and no pulses (the fabrication bug); a real
  `openStatusChangedAt` still writes it once
- `_describe_api_error`'s default `entity="camera"` keeps existing
  camera-only wording unchanged; a sensor 404 says "sensor not found", not
  "camera not found"
- `Devices.xml`: all four issue #8 device types (`protectSensor`,
  `protectLight`, `protectChime`, `protectNvr`) declare the hidden
  `SupportsStatusRequest` field
- `setChimeVolume` on a cache miss `GET`s the chime before ever reporting
  "no ringSettings"; the PATCH body carries only the four documented
  `ringSettings` keys, dropping any extra field a cached entry might carry
- a bool battery percentage is skipped, not written as a fabricated
  battery level
- sensor `lastMotion` merges the poll's `motionDetectedAt` with the
  tracker's own timestamp (newest wins), proven both when only the poll
  has one (post-restart) and when the tracker's is newer
- `lastLeak`/`lastAlarm`/`lastTamper` are written from the poll's own
  timestamps and skipped when null; a dedicated test pins the recovery
  limitation directly: `leakDetected` reads `False` post-restart while
  `lastLeak` still shows the poll's `leakDetectedAt`
