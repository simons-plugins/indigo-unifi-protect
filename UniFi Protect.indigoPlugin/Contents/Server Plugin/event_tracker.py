"""Folds the UniFi Protect WebSocket event stream into per-camera motion state.

Pure logic only: no Indigo imports, no network, no file I/O. `handle()` must
never raise, even on malformed input, so the caller (plugin.py's read loop)
can feed it directly off the wire.

See docs/CONTRACT.md, section `event_tracker.py`, for the binding spec.
"""

from collections import deque


class EventTracker:
    """Tracks Protect smartDetectZone events and derives per-camera motion state.

    An event lifecycle is: `add` (has `start`, no `end`) -> zero or more
    `update` keepalives (no `end`) -> `update` carrying `end`. A camera is
    "active" while at least one of its events has not yet received its `end`.

    `handle()` reports a camera as changed when EITHER of two independent
    things changed as a result of the message: the camera's active/idle flag,
    or the union of detect types (person/vehicle/animal/...) across its
    active events. A camera with an active "person" event that then also
    picks up an active "vehicle" event does not flip active/idle, but its
    detect-type union did change and callers (e.g. plugin.py, which only
    writes device states for cameras in the returned set) need to know.

    Traps this class exists to prevent:

    1. Terminal frames repeat on the wire (the same `end` can arrive 2-3x).
       A repeat must be a no-op.
    2. A stale no-`end` keepalive can arrive for an id that has already
       finished (out-of-order delivery, or a keepalive queued before the
       `end` was processed). Once an id is finished it MUST stay finished —
       an `add`/`update` without `end` for a finished id is ignored outright,
       never treated as "active".
    3. A cosmetic field (`start`) or a malformed `smartDetectTypes` element
       must never be allowed to discard a frame that carries a lifecycle
       signal (`end`). Both are tolerated/degraded rather than treated as
       fatal, and a frame that genuinely cannot be parsed at all is counted
       (`malformed_count`, and `dropped_terminal_count` when it carried an
       `end`) rather than silently swallowed.
    """

    def __init__(self, finished_cap: int = 512) -> None:
        self._finished_cap = finished_cap
        self._finished_ids: set = set()
        self._finished_order: deque = deque()

        # camera_id -> {event_id: frozenset(smartDetectTypes)} for events
        # that are currently active (no `end` seen yet) on that camera.
        self._active_events: dict = {}

        # camera_id -> most recent epoch-ms `start` seen for that camera,
        # active or not.
        self._last_motion: dict = {}

        # Diagnostic counters. Not reset by reset() — they describe stream
        # health over the tracker's lifetime, not live per-camera state.
        self._malformed_count = 0
        self._dropped_terminal_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, message: dict) -> set:
        """Apply one WS message. Return the set of camera ids that changed.

        Never raises: any malformed input is silently ignored and yields an
        empty set, which is also the correct (no-op) result for a duplicate
        or stale frame. The two cases are NOT the same thing and an empty
        set alone can't tell them apart — `malformed_count` and
        `dropped_terminal_count` are the voice for "a frame was destroyed
        and may have lost an event", as opposed to "nothing changed".
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
        discarded (as opposed to tolerated/degraded and still processed)."""
        return self._malformed_count

    @property
    def dropped_terminal_count(self) -> int:
        """Subset of `malformed_count` where the discarded frame's `item`
        carried an `end` — i.e. a lifecycle signal was actually lost, not
        just a keepalive."""
        return self._dropped_terminal_count

    def is_active(self, camera_id: str) -> bool:
        """True while >=1 unfinished event references this camera."""
        return bool(self._active_events.get(camera_id))

    def detect_types(self, camera_id: str) -> set:
        """Union of smartDetectTypes across this camera's active events."""
        return self._union_types(self._active_events.get(camera_id) or {})

    def last_motion_ms(self, camera_id: str):
        """Epoch-ms `start` of the most recent event seen for this camera."""
        return self._last_motion.get(camera_id)

    def clear_camera(self, camera_id: str) -> bool:
        """Force a camera idle (used on socket loss).

        Returns True if it was active. Does not touch `_last_motion` or the
        finished-id cache — this is a connectivity-loss reset of live state,
        not a semantic "these events never happened".
        """
        was_active = bool(self._active_events.get(camera_id))
        self._active_events[camera_id] = {}
        return was_active

    def reset(self) -> None:
        """Drop all state. Used on reconnect."""
        self._finished_ids.clear()
        self._finished_order.clear()
        self._active_events.clear()
        self._last_motion.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _handle(self, message: dict) -> set:
        if not isinstance(message, dict):
            self._malformed_count += 1
            return set()

        item = message.get("item")
        parsed = self._parse_item(item)
        if parsed is None:
            self._malformed_count += 1
            if isinstance(item, dict) and item.get("end") is not None:
                self._dropped_terminal_count += 1
            return set()
        device, event_id, types, start, end = parsed

        if end is not None:
            result = self._finish(device, event_id)
        else:
            result = self._activate(device, event_id, types)

        # Bookkeeping only, and best-effort: a bad/missing `start` must
        # never gate a lifecycle operation, so this runs AFTER dispatch,
        # not before.
        if start is not None:
            prev = self._last_motion.get(device)
            if prev is None or start > prev:
                self._last_motion[device] = start

        return result

    @staticmethod
    def _parse_item(item):
        """Validate + extract fields from a message's `item`. None if malformed.

        `device` and `id` are the only fields that can make a frame
        unidentifiable — those stay hard requirements. `start` and
        `smartDetectTypes` are best-effort: a bad value there degrades
        rather than discarding the frame, because a bad cosmetic field must
        never be allowed to veto a lifecycle signal (`end`).
        """
        if not isinstance(item, dict):
            return None

        device = item.get("device")
        if not isinstance(device, str) or not device:
            return None

        event_id = item.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None

        types = EventTracker._safe_type_set(item.get("smartDetectTypes"))

        start = item.get("start")
        if not isinstance(start, int):
            start = None

        end = item.get("end")
        return device, event_id, types, start, end

    @staticmethod
    def _safe_type_set(raw) -> set:
        """Best-effort extraction of smartDetectTypes.

        A missing/non-list value, or a list containing an unhashable
        element (e.g. `{"type": "person"}` instead of `"person"`), degrades
        to an empty set rather than raising — never let a types problem
        discard the frame carrying it.
        """
        if not isinstance(raw, list):
            return set()
        try:
            return set(raw)
        except TypeError:
            return set()

    @staticmethod
    def _union_types(cam_events: dict) -> set:
        union: set = set()
        for types in cam_events.values():
            union |= types
        return union

    @staticmethod
    def _message_has_end(message) -> bool:
        """Best-effort, never-raising check for whether a message's item
        carried an `end` — used only to classify an already-caught
        exception for the diagnostic counters."""
        try:
            item = message.get("item")
            return isinstance(item, dict) and item.get("end") is not None
        except Exception:
            return False

    def _activate(self, device: str, event_id: str, types: set) -> set:
        # Trap 2: an id that has already finished must never be reactivated
        # by a stale/out-of-order keepalive, no matter how it arrives.
        if event_id in self._finished_ids:
            return set()

        cam_events = self._active_events.setdefault(device, {})
        was_active = bool(cam_events)
        before_types = self._union_types(cam_events)

        cam_events[event_id] = frozenset(types)

        after_types = self._union_types(cam_events)

        # Changed if EITHER the active flag flipped OR the detect-type
        # union moved — a second concurrent detect type (e.g. a vehicle
        # joining an already-active person event) doesn't flip active/idle
        # but callers still need to know.
        if (not was_active) or (before_types != after_types):
            return {device}
        return set()

    def _finish(self, device: str, event_id: str) -> set:
        cam_events = self._active_events.get(device)
        was_tracked_active = cam_events is not None and event_id in cam_events

        changed: set = set()
        if was_tracked_active:
            before_types = self._union_types(cam_events)
            del cam_events[event_id]
            is_active_now = bool(cam_events)
            after_types = self._union_types(cam_events) if is_active_now else set()

            # Changed if the active flag flipped (camera went idle) OR the
            # remaining detect-type union moved (e.g. a person event ended
            # while a vehicle event on the same camera is still open).
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
