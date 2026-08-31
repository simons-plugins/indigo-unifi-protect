"""Folds the UniFi Protect WebSocket event stream into per-device state.

Pure logic only: no Indigo imports, no network, no file I/O. `handle()` must
never raise, even on malformed input, so the caller (plugin.py's read loop)
can feed it directly off the wire.

Every event family this module knows about is one of two shapes:

- **Lifecycle** families (`add` with `start`, zero or more `update`
  keepalives, then an `update` carrying `end`): camera MOTION, camera AUDIO
  (speech, baby cry, smoke alarm, CO alarm), Protect-sensor `sensorMotion`,
  `sensorWaterLeak`, `sensorAlarm`, and `sensorTamper`. Each is tracked
  completely independently per device -- folding one into another is exactly
  the bug this module exists to prevent (an audio "speech" event was once
  read as motion because nothing inspected `item.type` at all).
- **Pulse** types (`sensorOpened`, `sensorClosed`, `sensorBatteryLow`,
  `sensorExtremeValues`, `sensorSmokeTest`, `lightMotion`): fire-once
  notifications, per each type's own OpenAPI description -- Protect's spec
  gives each of these a nullable `end`, but the type's own description
  ("has entered an open state", "is getting low", "has encountered
  motion") is a point-in-time notice, not an active/idle span. NOTHING in
  this bullet has ever been observed on any wire, the reference rig's or
  otherwise -- it has no Protect sensors or lights at all. They are
  recorded as the most recent sighting per device+type (`last_pulse`),
  deduped by event id the same way a lifecycle `end` is, and never gate an
  active/idle flag.

Camera MOTION/AUDIO were verified live on 2026-08-26 against a UNVR at
192.168.0.10 running Protect 7.2.105 (see docs/CONTRACT.md). The four
Protect-sensor lifecycle families and all six pulse types are taken from
Protect's published OpenAPI spec (v6.2.83) -- the reference rig's
`GET /sensors`/`/lights`/`/chimes` all return `[]`, so NONE of this has been
observed on the wire. They share the exact same add/update/end (or
fire-once) envelope as the proven camera families, so they are routed
identically; see docs/CONTRACT.md for the "UNVERIFIED -- spec-derived"
marker.

See docs/CONTRACT.md, section `event_tracker.py`, for the binding spec.
"""

from collections import deque

# item.type values. Source: UniFi Protect Integration API OpenAPI spec
# v6.2.83 (developer.ui.com), vendored as docs/protect-openapi-v6.2.83.json
# by PR #10; verified against Protect 7.2.105. Only smartDetectZone and
# smartAudioDetect have been observed live on the reference rig (see
# docs/CONTRACT.md); motion, smartDetectLine and smartDetectLoiterZone come
# from the spec, not proven on the wire, but share the same add/update/end
# lifecycle so they are routed identically. `motion` is the plain (non-smart)
# camera motion event and carries no smartDetectTypes at all -- that is
# expected, not malformed.
MOTION_EVENT_TYPES = frozenset({
    "motion", "smartDetectZone", "smartDetectLine", "smartDetectLoiterZone",
})
AUDIO_EVENT_TYPES = frozenset({"smartAudioDetect"})

# Protect-sensor lifecycle families (issue #8). Spec-derived only -- the
# reference rig has no Protect sensors, so none of this has been observed on
# the wire. Each shares the camera families' add/update/end envelope.
SENSOR_MOTION_EVENT_TYPES = frozenset({"sensorMotion"})
SENSOR_LEAK_EVENT_TYPES = frozenset({"sensorWaterLeak"})
SENSOR_ALARM_EVENT_TYPES = frozenset({"sensorAlarm"})
SENSOR_TAMPER_EVENT_TYPES = frozenset({"sensorTamper"})

# Pulse types (issue #8): fire-once notifications, never an active/idle span.
# Spec-derived only, same caveat as the sensor lifecycle families above.
PULSE_EVENT_TYPES = frozenset({
    "sensorOpened", "sensorClosed", "sensorBatteryLow", "sensorExtremeValues",
    "sensorSmokeTest", "lightMotion",
})

# Documented event types this plugin does not act on at all. Doorbell rings
# are the only one left -- every other previously-unsupported type (Protect
# sensors, floodlight motion) is now a recognized lifecycle or pulse type
# above. Ignored and counted exactly like a genuinely unrecognized type; the
# distinction exists only so plugin.py can log these at a lower level than a
# type nobody has ever heard of.
KNOWN_UNSUPPORTED_EVENT_TYPES = frozenset({"ring"})

# Internal family keys. Every public method below is already family-specific
# by name (is_active/audio_active, etc.) for the two original camera
# families; the four Protect-sensor families are reached only through the
# generic family_*() methods, using these same string values as the `family`
# argument.
FAMILY_MOTION = "motion"
FAMILY_AUDIO = "audio"
FAMILY_SENSOR_MOTION = "sensorMotion"
FAMILY_SENSOR_LEAK = "sensorLeak"
FAMILY_SENSOR_ALARM = "sensorAlarm"
FAMILY_SENSOR_TAMPER = "sensorTamper"

# Backward-compat internal aliases -- the original private names, kept so the
# camera-specific code below (predating the generic family_*() methods) does
# not need to change.
_FAMILY_MOTION = FAMILY_MOTION
_FAMILY_AUDIO = FAMILY_AUDIO

_LIFECYCLE_FAMILIES = (
    FAMILY_MOTION, FAMILY_AUDIO, FAMILY_SENSOR_MOTION, FAMILY_SENSOR_LEAK,
    FAMILY_SENSOR_ALARM, FAMILY_SENSOR_TAMPER,
)

_TYPE_TO_FAMILY = {t: FAMILY_MOTION for t in MOTION_EVENT_TYPES}
_TYPE_TO_FAMILY.update({t: FAMILY_AUDIO for t in AUDIO_EVENT_TYPES})
_TYPE_TO_FAMILY.update({t: FAMILY_SENSOR_MOTION for t in SENSOR_MOTION_EVENT_TYPES})
_TYPE_TO_FAMILY.update({t: FAMILY_SENSOR_LEAK for t in SENSOR_LEAK_EVENT_TYPES})
_TYPE_TO_FAMILY.update({t: FAMILY_SENSOR_ALARM for t in SENSOR_ALARM_EVENT_TYPES})
_TYPE_TO_FAMILY.update({t: FAMILY_SENSOR_TAMPER for t in SENSOR_TAMPER_EVENT_TYPES})

# Key `ignored_type_counts` is bucketed under when item.type was absent or
# not a string -- distinct from any real (if unrecognized) type string.
MISSING_TYPE_KEY = "<missing>"

# Cap on distinct keys tracked by ignored_type_counts/ignored_type_samples.
# A stream sending an unbounded variety of item.type strings (or a
# misbehaving one) would otherwise grow these dicts without bound; past the
# cap, every NEW key is folded into OTHER_TYPE_KEY instead.
MAX_IGNORED_TYPE_KEYS = 64
OTHER_TYPE_KEY = "<other>"


def _extract_smart_detect_types(item):
    """Type-extractor for MOTION/AUDIO: the `smartDetectTypes` list."""
    return EventTracker._safe_type_set(item.get("smartDetectTypes"))


def _extract_alarm_types(item):
    """Type-extractor for sensorAlarm: `metadata.alarmType.text`, per the
    OpenAPI spec's `sensorAlarmEvent` schema -- there is no
    `smartDetectTypes` on this event at all. A missing/malformed metadata
    shape degrades to an empty set rather than raising, same as
    `_safe_type_set` does for the camera families."""
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return set()
    alarm_type = metadata.get("alarmType")
    if not isinstance(alarm_type, dict):
        return set()
    text = alarm_type.get("text")
    if isinstance(text, str) and text:
        return {text}
    return set()


def _extract_no_types(item):
    """Type-extractor for families whose event schema has no per-event type
    union at all (sensorMotion, sensorWaterLeak, sensorTamper) -- always
    empty."""
    return set()


_FAMILY_TYPE_EXTRACTOR = {
    FAMILY_MOTION: _extract_smart_detect_types,
    FAMILY_AUDIO: _extract_smart_detect_types,
    FAMILY_SENSOR_MOTION: _extract_no_types,
    FAMILY_SENSOR_LEAK: _extract_no_types,
    FAMILY_SENSOR_ALARM: _extract_alarm_types,
    FAMILY_SENSOR_TAMPER: _extract_no_types,
}


class EventTracker:
    """Tracks Protect lifecycle events and pulses, deriving per-device state.

    An event lifecycle is: `add` (has `start`, no `end`) -> zero or more
    `update` keepalives (no `end`) -> `update` carrying `end`. A device is
    "active" (for a given lifecycle family) while at least one of its events
    in that family has not yet received its `end`. Every lifecycle family is
    tracked independently per device -- an active event in one family never
    affects any other family's active/idle flag.

    A wire fact worth knowing before touching this code: a `smartAudioDetect`
    `add` frame carries an EMPTY `smartDetectTypes` list. The actual
    classification (e.g. `alrmSpeak`) arrives on the first `update` roughly a
    second later. This is not malformed -- `audio_active` is true from the
    `add` onward, with `audio_types` empty until that update lands. See
    `tests/fixtures/ws_capture_audio.json`, frames 3-5.

    `item.type` values this tracker does not recognize (missing, non-string,
    or simply not one of the known lifecycle families or pulse types) are
    IGNORED: no state changes anywhere, and the frame is counted in
    `ignored_type_counts` rather than folded into any family's state.

    `handle()` reports a device as changed when ANY of the following moved
    as a result of the message: a lifecycle family's active/idle flag, that
    family's detect-type union, or a pulse type's `last_pulse` value. A
    device with an active "person" motion event that then also picks up an
    active "vehicle" motion event does not flip active/idle, but its
    detect-type union did change, and callers (e.g. plugin.py, which only
    writes device states for ids in the returned set) need to know.

    Traps this class exists to prevent:

    1. Terminal frames repeat on the wire (the same `end` can arrive 2-3x).
       A repeat must be a no-op. The same applies to a repeated pulse frame.
    2. A stale no-`end` keepalive can arrive for an id that has already
       finished (out-of-order delivery, or a keepalive queued before the
       `end` was processed). Once an id is finished it MUST stay finished --
       an `add`/`update` without `end` for a finished id is ignored outright,
       never treated as "active". This applies identically across every
       lifecycle family.
    3. A cosmetic field (`start`) or a malformed `smartDetectTypes`/metadata
       element must never be allowed to discard a frame that carries a
       lifecycle signal (`end`). Both are tolerated/degraded rather than
       treated as fatal, and a frame that genuinely cannot be parsed at all
       is counted (`malformed_count`, and `dropped_terminal_count` when it
       carried an `end`) rather than silently swallowed. `_safe_type_set`
       keeps only string elements (an unhashable dict, a stray int, `None`,
       ...) rather than raising OR keeping a mixed list -- a non-string
       element surviving into a device's type set would otherwise reach
       plugin.py's `sorted()`/`",".join()` calls and raise `TypeError`
       there instead, which would escape `_pump` and tear the event socket
       down. Enforced here, not trusted to the caller.
    4. An unrecognized `item.type` must never be folded into any family's
       state -- it is counted (`ignored_type_counts`) and otherwise ignored,
       not treated as a malformed frame and not treated as any known family.
       But this must NOT extend to an `end` frame for an id the tracker is
       already holding: a missing/wrong-family `item.type` on that specific
       frame must not be allowed to lose the lifecycle signal and leave the
       device stuck active forever with no diagnostic -- see `_finish`,
       which resolves an event's real family/device from `_active_index`,
       never from the terminating frame's own (possibly missing or wrong)
       `item.type`.

    Event ids are globally unique across every family, lifecycle or pulse
    (motion ids are UUIDs, audio ids are 24-char hex; the OpenAPI spec gives
    every event, including Protect-sensor and pulse events, the same shared
    `eventId` schema), so the finished-id cache, the active-event index
    (`_active_index`, event_id -> (family, device)), and pulse dedup are all
    shared across every family rather than kept separately.
    """

    def __init__(self, finished_cap: int = 512) -> None:
        self._finished_cap = finished_cap
        self._finished_ids: set = set()
        self._finished_order: deque = deque()

        # family -> device_id -> {event_id: frozenset(types)} for events
        # currently active (no `end` seen yet) on that device, in that
        # family. One entry per lifecycle family in _LIFECYCLE_FAMILIES.
        self._active_events: dict = {family: {} for family in _LIFECYCLE_FAMILIES}

        # event_id -> (family, device) for every event currently present in
        # _active_events. Shared across every lifecycle family (ids are
        # unique) so `_finish` can resolve an event's REAL family/device
        # without trusting the terminating frame's own (possibly missing or
        # wrong) item.type -- see class docstring, trap 4.
        self._active_index: dict = {}

        # family -> device_id -> most recent epoch-ms `start` seen for that
        # device in that family, active or not.
        self._last_seen: dict = {family: {} for family in _LIFECYCLE_FAMILIES}

        # pulse_type -> device_id -> {"start": ms|None, "metadata": dict}.
        # Survives clear_camera()/clear_family() (it is a history, not live
        # state); dropped only by reset().
        self._pulses: dict = {}

        # Diagnostic counters. Not reset by reset() -- they describe stream
        # health over the tracker's lifetime, not live per-device state.
        self._malformed_count = 0
        self._dropped_terminal_count = 0
        self._ignored_type_counts: dict = {}
        # key -> first device id seen sending that ignored type. Lets a log
        # line point at which device actually sent it.
        self._ignored_type_samples: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, message: dict) -> set:
        """Apply one WS message. Return the set of device ids that changed.

        Never raises: any malformed input is silently ignored and yields an
        empty set, which is also the correct (no-op) result for a duplicate
        or stale frame, or a frame of an unrecognized `item.type`. These
        cases are NOT the same thing and an empty set alone can't tell them
        apart -- `malformed_count`, `dropped_terminal_count`, and
        `ignored_type_counts` are the voice for "a frame was destroyed and
        may have lost an event" and "a frame arrived for an event family we
        don't act on", as opposed to "nothing changed".
        """
        try:
            return self._handle(message)
        except Exception:
            self._malformed_count += 1
            if self._message_has_end(message):
                self._dropped_terminal_count += 1
            return set()

    @property
    def malformed_count(self) -> int:
        """Count of messages that could not be parsed at all and were
        discarded (as opposed to tolerated/degraded and still processed, or
        ignored because `item.type` wasn't a family this tracker acts on)."""
        return self._malformed_count

    @property
    def dropped_terminal_count(self) -> int:
        """Subset of `malformed_count` where the discarded frame's `item`
        carried an `end` -- i.e. a lifecycle signal was actually lost, not
        just a keepalive."""
        return self._dropped_terminal_count

    @property
    def ignored_type_counts(self) -> dict:
        """Count of frames whose `item.type` was not a recognized family or
        pulse type, keyed by the type string (`"<missing>"` when absent or
        not a string). These frames parsed fine -- they are NOT malformed --
        they just aren't something this tracker folds into device state.
        Capped at `MAX_IGNORED_TYPE_KEYS` distinct keys; anything past that
        is folded into `OTHER_TYPE_KEY`. A copy, so callers can't mutate
        tracker-internal state through it."""
        return dict(self._ignored_type_counts)

    @property
    def ignored_type_samples(self) -> dict:
        """First-seen device id for each key in `ignored_type_counts` --
        `"unknown"` when the frame's own `device` field was itself
        missing/invalid. Lets a log line point at which device actually
        sent an ignored type. A copy, for the same reason
        `ignored_type_counts` returns one."""
        return dict(self._ignored_type_samples)

    def is_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished MOTION event references this camera."""
        return self._is_active(_FAMILY_MOTION, camera_id)

    def detect_types(self, camera_id: str) -> set:
        """Union of smartDetectTypes across this camera's active MOTION
        events. Empty set when idle."""
        return self._types(_FAMILY_MOTION, camera_id)

    def last_motion_ms(self, camera_id: str):
        """Epoch-ms `start` of the most recent MOTION event seen for this
        camera, active or not. None if never seen."""
        return self._last_seen[_FAMILY_MOTION].get(camera_id)

    def audio_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished AUDIO event references this camera.

        An audio event counts as active from its `add` onward even before
        it is classified -- the `add` frame's `smartDetectTypes` was empty
        in the one live capture (frame 3); the code does not rely on it,
        with the real types arriving on a later `update`."""
        return self._is_active(_FAMILY_AUDIO, camera_id)

    def audio_types(self, camera_id: str) -> set:
        """Union of smartDetectTypes across this camera's active AUDIO
        events. Empty set when idle, and also empty for an active-but-not-
        yet-classified audio event."""
        return self._types(_FAMILY_AUDIO, camera_id)

    def last_audio_ms(self, camera_id: str):
        """Epoch-ms `start` of the most recent AUDIO event seen for this
        camera, active or not. None if never seen."""
        return self._last_seen[_FAMILY_AUDIO].get(camera_id)

    # -- Generic lifecycle-family access (issue #8) ---------------------
    #
    # Reaches every lifecycle family, including the two camera families
    # above (FAMILY_MOTION/FAMILY_AUDIO) as well as the four Protect-sensor
    # families (FAMILY_SENSOR_MOTION/_LEAK/_ALARM/_TAMPER). The camera-
    # specific methods above are NOT reimplemented on top of these -- they
    # predate this generalization and their behavior is pinned by the
    # existing test suite -- but they operate on the exact same underlying
    # per-family storage, so the two APIs never disagree.

    def family_active(self, family: str, device_id: str) -> bool:
        """True while >=1 unfinished event in `family` references this
        device. `family` is one of the FAMILY_* module constants."""
        return self._is_active(family, device_id)

    def family_types(self, family: str, device_id: str) -> set:
        """Union of this family's per-event type set across this device's
        active events. Empty set when idle, and always empty for a family
        whose events carry no per-event type (sensorMotion, sensorWaterLeak,
        sensorTamper)."""
        return self._types(family, device_id)

    def last_family_ms(self, family: str, device_id: str):
        """Epoch-ms `start` of the most recent event seen for this device in
        `family`, active or not. None if never seen."""
        return self._last_seen[family].get(device_id)

    def clear_family(self, family: str, device_id: str) -> bool:
        """Force one device idle in one lifecycle family (used on socket
        loss). Returns True if that family was active. Does not touch
        `_last_seen`, the finished-id cache, `_active_index`, or any pulse
        -- this is a connectivity-loss reset of live state, not a semantic
        "this never happened" (matches clear_camera's existing behavior,
        which never cleaned up `_active_index` either -- a stale index
        entry is harmless: `_finish` guards on the event id still being
        present in `_active_events` before acting on it)."""
        was_active = bool(self._active_events[family].get(device_id))
        self._active_events[family][device_id] = {}
        return was_active

    def last_pulse(self, device_id: str, pulse_type: str):
        """The most recently recorded pulse of `pulse_type` for this device,
        as `{"start": epoch-ms|None, "metadata": dict}`, or None if this
        device has never sent that pulse type. `pulse_type` is one of the
        PULSE_EVENT_TYPES strings (e.g. "sensorBatteryLow")."""
        return self._pulses.get(pulse_type, {}).get(device_id)

    def clear_camera(self, camera_id: str) -> bool:
        """Force a camera idle in BOTH families (used on socket loss).

        Returns True if EITHER family was active. Does not touch
        `_last_seen` or the finished-id cache -- this is a connectivity-loss
        reset of live state, not a semantic "these events never happened".
        """
        was_motion_active = self.clear_family(_FAMILY_MOTION, camera_id)
        was_audio_active = self.clear_family(_FAMILY_AUDIO, camera_id)
        return was_motion_active or was_audio_active

    def reset(self) -> None:
        """Drop all state, every family and every pulse. Used on reconnect."""
        self._finished_ids.clear()
        self._finished_order.clear()
        for family in _LIFECYCLE_FAMILIES:
            self._active_events[family].clear()
            self._last_seen[family].clear()
        self._active_index.clear()
        self._pulses.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_active(self, family: str, device_id: str) -> bool:
        return bool(self._active_events[family].get(device_id))

    def _types(self, family: str, device_id: str) -> set:
        return self._union_types(self._active_events[family].get(device_id) or {})

    def _handle(self, message: dict) -> set:
        if not isinstance(message, dict):
            self._malformed_count += 1
            return set()

        item = message.get("item")
        if not isinstance(item, dict):
            self._malformed_count += 1
            return set()

        # A held id's `end` must finish that event no matter what item.type
        # says. A real bug let an audio-typed (or type-missing) end frame
        # for a MOTION id fall straight into the ignore-and-count branch
        # below and get lost with zero diagnostics, leaving the device
        # stuck active forever. `_active_index` is authoritative for "which
        # family is this id actually tracked under" -- the frame's own type
        # is not. Runs BEFORE type classification/ignoring/pulse-routing on
        # purpose; only fires for an id we are actually holding as a
        # LIFECYCLE event, so a pulse id (never added to _active_index) is
        # unaffected and still routes through the pulse branch below.
        event_id = item.get("id")
        if (item.get("end") is not None and isinstance(event_id, str)
                and event_id in self._active_index):
            return self._finish(event_id)

        raw_type = item.get("type")

        if isinstance(raw_type, str) and raw_type in PULSE_EVENT_TYPES:
            return self._handle_pulse(raw_type, item)

        family = _TYPE_TO_FAMILY.get(raw_type) if isinstance(raw_type, str) else None
        if family is None:
            # Not malformed: the frame parsed fine, it just isn't a family
            # this tracker acts on. Return immediately -- an unrecognized
            # type must never reach _parse_item/_activate, so it can never
            # be folded into any family's state.
            key = raw_type if isinstance(raw_type, str) else MISSING_TYPE_KEY
            self._count_ignored(key, item)
            return set()

        parsed = self._parse_item(item, family)
        if parsed is None:
            self._malformed_count += 1
            if item.get("end") is not None:
                self._dropped_terminal_count += 1
            return set()
        device, event_id, types, start, end = parsed

        if end is not None:
            result = self._finish(event_id)
        else:
            result = self._activate(family, device, event_id, types)

        # Bookkeeping only, and best-effort: a bad/missing `start` must
        # never gate a lifecycle operation, so this runs AFTER dispatch,
        # not before.
        if start is not None:
            last_seen = self._last_seen[family]
            prev = last_seen.get(device)
            if prev is None or start > prev:
                last_seen[device] = start

        return result

    def _handle_pulse(self, pulse_type: str, item: dict) -> set:
        """Record one fire-once pulse. Never raises -- a malformed pulse
        frame (missing device/id) is counted in `malformed_count` exactly
        like a malformed lifecycle frame, but never `dropped_terminal_count`:
        a pulse has no `end` to lose."""
        device = item.get("device")
        event_id = item.get("id")
        if not isinstance(device, str) or not device \
                or not isinstance(event_id, str) or not event_id:
            self._malformed_count += 1
            return set()

        # Dedupe by id, shared with the lifecycle finished-id cache -- ids
        # are globally unique across every family, so a repeat pulse frame
        # (the wire fact proven for lifecycle `end` frames, and expected to
        # hold here too) is a pure no-op.
        if event_id in self._finished_ids:
            return set()

        start = item.get("start")
        if not isinstance(start, int):
            start = None
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        self._pulses.setdefault(pulse_type, {})[device] = {
            "start": start, "metadata": metadata,
        }
        self._mark_finished(event_id)
        return {device}

    def _count_ignored(self, key: str, item: dict) -> None:
        """Bump `ignored_type_counts[key]` and, the first time this key is
        seen, record which device sent it. See MAX_IGNORED_TYPE_KEYS for the
        distinct-key cap."""
        if key not in self._ignored_type_counts and len(self._ignored_type_counts) >= MAX_IGNORED_TYPE_KEYS:
            key = OTHER_TYPE_KEY
        self._ignored_type_counts[key] = self._ignored_type_counts.get(key, 0) + 1
        if key not in self._ignored_type_samples:
            device = item.get("device")
            self._ignored_type_samples[key] = device if isinstance(device, str) and device else "unknown"

    @staticmethod
    def _parse_item(item, family):
        """Validate + extract fields from a message's `item`. None if malformed.

        `device` and `id` are the only fields that can make a frame
        unidentifiable -- those stay hard requirements. `start` and the
        family's type union are best-effort: a bad value there degrades
        rather than discarding the frame, because a bad cosmetic field must
        never be allowed to veto a lifecycle signal (`end`). The caller has
        already validated `item` is a dict and resolved its family.
        """
        device = item.get("device")
        if not isinstance(device, str) or not device:
            return None

        event_id = item.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None

        types = _FAMILY_TYPE_EXTRACTOR[family](item)

        start = item.get("start")
        if not isinstance(start, int):
            start = None

        end = item.get("end")
        return device, event_id, types, start, end

    @staticmethod
    def _safe_type_set(raw) -> set:
        """Best-effort extraction of smartDetectTypes.

        Only string elements are kept; anything else (an unhashable dict
        like `{"type": "person"}`, a stray int, `None`, ...) is dropped
        rather than raising or being kept as-is. A non-string element that
        survived into this set would otherwise reach plugin.py's
        `_write_states` -- `sorted()` and `",".join()` there raise
        `TypeError` on a mixed list, which would escape `_pump`, tear the
        event socket down, and wipe every camera's live state on
        reconnect. That is the opposite of what trap 3 exists to prevent,
        so it is enforced here instead of trusted to the caller. A
        missing/non-list value degrades to an empty set the same way -- a
        plain `motion` event has no smartDetectTypes key at all.
        """
        if not isinstance(raw, list):
            return set()
        return {element for element in raw if isinstance(element, str)}

    @staticmethod
    def _union_types(cam_events: dict) -> set:
        union: set = set()
        for types in cam_events.values():
            union |= types
        return union

    @staticmethod
    def _message_has_end(message) -> bool:
        """Best-effort, never-raising check for whether a message's item
        carried an `end` -- used only to classify an already-caught
        exception for the diagnostic counters."""
        try:
            item = message.get("item")
            return isinstance(item, dict) and item.get("end") is not None
        except Exception:
            return False

    def _activate(self, family: str, device: str, event_id: str, types: set) -> set:
        # Trap 2: an id that has already finished must never be reactivated
        # by a stale/out-of-order keepalive, no matter how it arrives.
        if event_id in self._finished_ids:
            return set()

        cam_events = self._active_events[family].setdefault(device, {})
        was_active = bool(cam_events)
        before_types = self._union_types(cam_events)

        cam_events[event_id] = frozenset(types)
        self._active_index[event_id] = (family, device)

        after_types = self._union_types(cam_events)

        # Changed if EITHER the active flag flipped OR the detect-type
        # union moved within this family -- a second concurrent detect type
        # (e.g. a vehicle joining an already-active person event) doesn't
        # flip active/idle but callers still need to know.
        if (not was_active) or (before_types != after_types):
            return {device}
        return set()

    def _finish(self, event_id: str) -> set:
        """Mark event_id finished, in whichever family/device the shared
        `_active_index` says it belongs to -- NOT whichever family the
        terminating frame itself claims. A frame's own `item.type` can be
        missing, wrong, or name the OTHER family; the index is built from
        that event's own `add`/`update` and is authoritative. An id not
        present in the index (never seen, or already finished) still gets
        marked finished below -- out-of-order arrival, or a repeat, must
        never create or re-finish an active event.
        """
        changed: set = set()
        indexed = self._active_index.get(event_id)

        if indexed is not None:
            family, device = indexed
            cam_events = self._active_events[family].get(device)
            if cam_events is not None and event_id in cam_events:
                before_types = self._union_types(cam_events)
                del cam_events[event_id]
                del self._active_index[event_id]
                is_active_now = bool(cam_events)
                after_types = self._union_types(cam_events) if is_active_now else set()

                # Changed if the active flag flipped (device went idle in
                # this family) OR the remaining detect-type union moved
                # (e.g. a person event ended while a vehicle event on the
                # same device is still open).
                if (not is_active_now) or (before_types != after_types):
                    changed.add(device)

        # Trap 1: terminal frames repeat. Only the first `end` for a given
        # id does anything; later ones must not re-run eviction bookkeeping
        # or be mistaken for a fresh finish.
        if event_id not in self._finished_ids:
            self._mark_finished(event_id)

        return changed

    def _mark_finished(self, event_id: str) -> None:
        self._finished_ids.add(event_id)
        self._finished_order.append(event_id)
        while len(self._finished_order) > self._finished_cap:
            oldest = self._finished_order.popleft()
            self._finished_ids.discard(oldest)
