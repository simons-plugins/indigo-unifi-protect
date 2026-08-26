"""Adversarial test suite for event_tracker.EventTracker.

The question these tests ask is not "does it detect motion?" but
"when could this report idle and be wrong, or report motion and never
clear?" — per docs/CONTRACT.md and workspace testing convention.

Real ground truth: tests/fixtures/ws_capture.json — 10 frames captured live
from a UNVR, two cameras, one person, proving two traps:

1. Terminal frames repeat (one event's `end` arrived 3x, the other's 2x).
2. A stale no-`end` keepalive can arrive for an id that has already
   finished; treated naively it re-arms the camera and it latches on
   forever.
"""

import json
import socket
import urllib.request
from pathlib import Path

import pytest

from event_tracker import EventTracker

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ws_capture.json"

CAMERA_A = "69ed1b24002f7f03e407c90a"  # event 9b5f3607-...
CAMERA_B = "69bea2c1001d4703e4002686"  # event bd8282c1-...


def load_capture():
    """Return the 10 real captured messages, in wire order."""
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        frames = json.load(f)
    return [f["message"] for f in frames]


# ---------------------------------------------------------------------
# The real 10-frame capture, replayed in order
# ---------------------------------------------------------------------

def test_real_capture_exact_transition_sequence():
    """Replay the live capture frame-by-frame and assert the exact
    active/idle transition points for both cameras — including that
    duplicate/repeated terminal frames produce an EMPTY changed set.
    """
    messages = load_capture()
    tracker = EventTracker()

    # (changed set, is_active(A), is_active(B)) expected after each frame.
    expected = [
        ({CAMERA_A}, True, False),   # 1: add 9b5f (A) -> A active
        (set(), True, False),        # 2: keepalive 9b5f -> no change
        ({CAMERA_B}, True, True),    # 3: add bd8282c1 (B) -> B active
        (set(), True, True),         # 4: keepalive bd8282c1 -> no change
        ({CAMERA_A}, False, True),   # 5: end 9b5f -> A idle
        (set(), False, True),        # 6: DUPLICATE end 9b5f -> no change
        (set(), False, True),        # 7: DUPLICATE end 9b5f -> no change
        (set(), False, True),        # 8: keepalive bd8282c1 (pre-end) -> no change
        ({CAMERA_B}, False, False),  # 9: end bd8282c1 -> B idle
        (set(), False, False),       # 10: DUPLICATE end bd8282c1 -> no change
    ]

    assert len(messages) == len(expected) == 10

    for frame_num, (message, (want_changed, want_a, want_b)) in enumerate(
        zip(messages, expected), start=1
    ):
        changed = tracker.handle(message)
        assert changed == want_changed, f"frame {frame_num}: changed set mismatch"
        assert tracker.is_active(CAMERA_A) is want_a, f"frame {frame_num}: camera A"
        assert tracker.is_active(CAMERA_B) is want_b, f"frame {frame_num}: camera B"

    # Final state: both idle, no detect types lingering.
    assert tracker.detect_types(CAMERA_A) == set()
    assert tracker.detect_types(CAMERA_B) == set()
    assert tracker.last_motion_ms(CAMERA_A) == 1787756557629
    assert tracker.last_motion_ms(CAMERA_B) == 1787756563350


def test_real_capture_duplicate_end_frames_are_pure_noops():
    """Isolate trap 1: replaying just the duplicate `end` frames (6, 7, 10)
    a second time, after the full capture has already been processed, must
    still produce empty changed sets and leave state untouched.
    """
    messages = load_capture()
    tracker = EventTracker()
    for message in messages:
        tracker.handle(message)

    assert tracker.is_active(CAMERA_A) is False
    assert tracker.is_active(CAMERA_B) is False

    # Replay every duplicate-end frame again.
    for idx in (5, 6, 9):  # frames 6, 7, 10 (0-indexed)
        changed = tracker.handle(messages[idx])
        assert changed == set()

    assert tracker.is_active(CAMERA_A) is False
    assert tracker.is_active(CAMERA_B) is False


# ---------------------------------------------------------------------
# Trap 2: stale keepalive arriving AFTER the id's own `end`
# ---------------------------------------------------------------------

def test_stale_keepalive_after_end_does_not_rearm():
    """The single most important line in the module: once an id is
    finished, a no-`end` keepalive for that same id (frame 8's message,
    replayed again after frames 9/10 have finished it) must be ignored
    outright — not treated as a fresh `add` that re-arms the camera.
    """
    messages = load_capture()
    tracker = EventTracker()
    for message in messages:
        tracker.handle(message)

    assert tracker.is_active(CAMERA_B) is False

    stale_keepalive = messages[7]  # frame 8: update, no end, bd8282c1
    changed = tracker.handle(stale_keepalive)

    assert changed == set(), "a stale post-end keepalive must not report a change"
    assert tracker.is_active(CAMERA_B) is False, "camera must not re-arm"
    assert tracker.detect_types(CAMERA_B) == set()


def test_stale_keepalive_after_end_synthetic_direct_order():
    """Same trap, minimal synthetic form: end arrives, THEN a no-end
    keepalive for the same id arrives immediately after (true stale
    delivery order, not just a replay)."""
    tracker = EventTracker()
    add = {"type": "add", "item": {"id": "evt-1", "device": "camX",
                                    "start": 1000, "smartDetectTypes": ["person"]}}
    end = {"type": "update", "item": {"id": "evt-1", "device": "camX",
                                       "start": 1000, "end": 2000,
                                       "smartDetectTypes": ["person"]}}
    stale = {"type": "update", "item": {"id": "evt-1", "device": "camX",
                                         "start": 1000, "smartDetectTypes": ["person"]}}

    assert tracker.handle(add) == {"camX"}
    assert tracker.handle(end) == {"camX"}
    assert tracker.is_active("camX") is False

    assert tracker.handle(stale) == set()
    assert tracker.is_active("camX") is False


# ---------------------------------------------------------------------
# Overlapping events on one camera (synthetic — not in the capture)
#
# BUG 1: `changed` must cover detect-type changes, not just active/idle
# transitions, or a caller that only writes states on `changed` (plugin.py)
# never sets vehicleDetected True / never clears personDetected while a
# person and a vehicle overlap on one camera.
# ---------------------------------------------------------------------

def test_two_overlapping_events_both_must_end_before_idle():
    tracker = EventTracker()
    cam = "camOverlap"

    add1 = {"item": {"id": "e1", "device": cam, "start": 100,
                      "smartDetectTypes": ["person"]}}
    add2 = {"item": {"id": "e2", "device": cam, "start": 150,
                      "smartDetectTypes": ["vehicle"]}}
    end1 = {"item": {"id": "e1", "device": cam, "start": 100, "end": 200,
                      "smartDetectTypes": ["person"]}}
    end2 = {"item": {"id": "e2", "device": cam, "start": 150, "end": 250,
                      "smartDetectTypes": ["vehicle"]}}

    assert tracker.handle(add1) == {cam}
    assert tracker.is_active(cam) is True
    assert tracker.detect_types(cam) == {"person"}

    # second event starts while the first is still active: active/idle
    # flag does NOT flip, but the detect-type union DOES (vehicle joins
    # person) — the camera must still be reported changed, or a caller
    # that only writes states on `changed` (plugin.py) never sets
    # vehicleDetected True. This is BUG 1.
    assert tracker.handle(add2) == {cam}
    assert tracker.is_active(cam) is True
    assert tracker.detect_types(cam) == {"person", "vehicle"}

    # first event ends: camera must STAY active because e2 is still open,
    # but the detect-type union shrank (person drops out) — must still be
    # reported changed, or personDetected never clears. Also BUG 1.
    assert tracker.handle(end1) == {cam}
    assert tracker.is_active(cam) is True
    assert tracker.detect_types(cam) == {"vehicle"}

    # second event ends: only now does the camera go idle.
    assert tracker.handle(end2) == {cam}
    assert tracker.is_active(cam) is False
    assert tracker.detect_types(cam) == set()


# ---------------------------------------------------------------------
# Out-of-order `end` for an id never seen as active
# ---------------------------------------------------------------------

def test_end_for_unknown_id_does_not_create_active_event():
    tracker = EventTracker()
    end = {"item": {"id": "never-seen", "device": "camY", "start": 50,
                     "end": 60, "smartDetectTypes": ["person"]}}

    changed = tracker.handle(end)

    assert changed == set()
    assert tracker.is_active("camY") is False
    assert tracker.detect_types("camY") == set()
    # last_motion is still recorded — the camera WAS seen, just never active.
    assert tracker.last_motion_ms("camY") == 50

    # And the id is now finished, so a later no-end keepalive for it must
    # also be ignored (out-of-order arrival must not resurrect it either).
    late_add = {"item": {"id": "never-seen", "device": "camY", "start": 50,
                          "smartDetectTypes": ["person"]}}
    assert tracker.handle(late_add) == set()
    assert tracker.is_active("camY") is False


# ---------------------------------------------------------------------
# Malformed input — must never raise, must never change state
#
# `device` and `id` are the only fields that can make a frame
# unidentifiable, so only THOSE are "malformed" (whole frame discarded).
# A missing/bad `smartDetectTypes` is no longer in this list — BUG 3 made
# it a tolerated degradation (empty type set) rather than a discard; see
# test_missing_smart_detect_types_degrades_to_empty_set_not_discarded and
# test_unhashable_smart_detect_type_element_does_not_discard_end below.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    {},
    {"type": "add"},                                  # missing 'item'
    {"type": "add", "item": None},                     # item not a dict
    {"type": "add", "item": "not-a-dict"},              # item not a dict
    {"type": "add", "item": []},                        # item not a dict
    {"type": "add", "item": {"id": "e1",
                              "smartDetectTypes": ["person"]}},  # missing device
    {"type": "add", "item": {"device": "camZ",
                              "smartDetectTypes": ["person"]}},  # missing id
    None,
    "not-a-dict-message",
    42,
])
def test_malformed_messages_ignored_never_raise(message):
    tracker = EventTracker()
    changed = tracker.handle(message)  # must not raise
    assert changed == set()
    assert tracker.is_active("camZ") is False


def test_malformed_messages_leave_prior_state_untouched():
    """A malformed frame arriving mid-stream must not disturb an already
    active camera's state."""
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "camZ", "start": 10,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {"camZ"}

    for bad in ({}, {"item": {}}, {"item": {"id": "e1"}}):
        assert tracker.handle(bad) == set()

    assert tracker.is_active("camZ") is True
    assert tracker.detect_types("camZ") == {"person"}


# ---------------------------------------------------------------------
# finished_cap eviction must not resurrect a recently-ended event
# ---------------------------------------------------------------------

def test_finished_cap_eviction_does_not_resurrect_recent_event():
    """Cap is small; fill it with exactly cap-1 OTHER finished ids after
    ending our event of interest, so it sits right at the eviction
    boundary without being evicted yet. A late keepalive for it must
    still be suppressed.
    """
    cap = 5
    tracker = EventTracker(finished_cap=cap)
    cam = "camCap"

    add = {"item": {"id": "target", "device": cam, "start": 1,
                     "smartDetectTypes": ["person"]}}
    end = {"item": {"id": "target", "device": cam, "start": 1, "end": 2,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}
    assert tracker.handle(end) == {cam}
    assert tracker.is_active(cam) is False

    # Fill up to (but not past) the cap with unrelated finished ids.
    for i in range(cap - 1):
        other_add = {"item": {"id": f"filler-{i}", "device": "camOther",
                               "start": 10 + i, "smartDetectTypes": ["person"]}}
        other_end = {"item": {"id": f"filler-{i}", "device": "camOther",
                               "start": 10 + i, "end": 11 + i,
                               "smartDetectTypes": ["person"]}}
        tracker.handle(other_add)
        tracker.handle(other_end)

    # "target" should still be within the cap -> a stale keepalive must
    # still be suppressed, not resurrect the camera.
    stale = {"item": {"id": "target", "device": cam, "start": 1,
                       "smartDetectTypes": ["person"]}}
    assert tracker.handle(stale) == set()
    assert tracker.is_active(cam) is False


def test_finished_cap_eviction_eventually_evicts_oldest():
    """Sanity check on the eviction mechanism itself: once enough NEWER
    finished ids push the cap past capacity, the oldest is evicted — this
    is documented, bounded-memory behaviour, not a bug. (A stale keepalive
    for the evicted id would then be treated as fresh; that is the
    accepted tradeoff of a bounded cache, not something the contract
    forbids.)
    """
    cap = 3
    tracker = EventTracker(finished_cap=cap)

    for i in range(cap + 2):
        eid = f"evt-{i}"
        tracker.handle({"item": {"id": eid, "device": "cam", "start": i,
                                  "smartDetectTypes": ["person"]}})
        tracker.handle({"item": {"id": eid, "device": "cam", "start": i,
                                  "end": i + 1, "smartDetectTypes": ["person"]}})

    assert len(tracker._finished_ids) == cap
    assert "evt-0" not in tracker._finished_ids
    assert "evt-1" not in tracker._finished_ids
    assert f"evt-{cap + 1}" in tracker._finished_ids


# ---------------------------------------------------------------------
# clear_camera / reset
# ---------------------------------------------------------------------

def test_clear_camera_forces_idle_and_reports_prior_state():
    tracker = EventTracker()
    cam = "camClear"
    add = {"item": {"id": "e1", "device": cam, "start": 1,
                     "smartDetectTypes": ["person"]}}
    tracker.handle(add)
    assert tracker.is_active(cam) is True

    assert tracker.clear_camera(cam) is True
    assert tracker.is_active(cam) is False
    # Calling again on an already-idle camera reports False.
    assert tracker.clear_camera(cam) is False


def test_reset_drops_all_state():
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "cam1", "start": 1,
                     "smartDetectTypes": ["person"]}}
    tracker.handle(add)
    assert tracker.is_active("cam1") is True
    assert tracker.last_motion_ms("cam1") == 1

    tracker.reset()

    assert tracker.is_active("cam1") is False
    assert tracker.last_motion_ms("cam1") is None

    # And the previously-active id is no longer "finished" either — reset
    # is a full wipe, so it can legitimately start a fresh lifecycle.
    assert tracker.handle(add) == {"cam1"}


# ---------------------------------------------------------------------
# Degradation-path test (workspace convention): the negative assertion
# must be FATAL, not a blessed empty result.
# ---------------------------------------------------------------------

def test_never_touches_the_network(monkeypatch):
    """Prove EventTracker is pure logic with no side channel to the
    network by making any such touch fatal, then running full normal
    operation (the real 10-frame capture) and asserting it completes
    exactly as expected regardless.
    """

    def _poisoned(*_args, **_kwargs):
        raise AssertionError(
            "EventTracker touched the network — it must be pure logic"
        )

    monkeypatch.setattr(socket.socket, "connect", _poisoned)
    monkeypatch.setattr(socket, "create_connection", _poisoned)
    monkeypatch.setattr(urllib.request, "urlopen", _poisoned)

    messages = load_capture()
    tracker = EventTracker()
    changed_per_frame = [tracker.handle(m) for m in messages]

    assert changed_per_frame == [
        {CAMERA_A}, set(), {CAMERA_B}, set(), {CAMERA_A},
        set(), set(), set(), {CAMERA_B}, set(),
    ]
    assert tracker.is_active(CAMERA_A) is False
    assert tracker.is_active(CAMERA_B) is False


# ---------------------------------------------------------------------
# BUG 2: a cosmetic `start` must never veto a lifecycle `end`.
# ---------------------------------------------------------------------

def test_non_int_start_on_terminal_frame_still_clears_camera():
    """A non-int `start` (e.g. a string) on the frame carrying `end` must
    not raise/veto the finish — the terminal frame must still be finishable
    on `device` + `id` alone. Reproduced: previously the blanket except
    swallowed a TypeError from comparing str > int in _last_motion
    bookkeeping, discarding the `end` and latching the camera on forever.
    """
    tracker = EventTracker()
    cam = "camBadStart"
    add = {"item": {"id": "e1", "device": cam, "start": 100,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}
    assert tracker.is_active(cam) is True

    end = {"item": {"id": "e1", "device": cam, "start": "100", "end": 200,
                     "smartDetectTypes": ["person"]}}
    changed = tracker.handle(end)

    assert changed == {cam}
    assert tracker.is_active(cam) is False
    # The bad `start` is ignored, not stored — last_motion keeps the last
    # valid int it saw.
    assert tracker.last_motion_ms(cam) == 100


def test_non_int_start_is_ignored_not_stored():
    """A non-int `start` on a non-terminal frame must not corrupt
    last_motion_ms either — it degrades to "no update this frame", not to
    storing the bad value."""
    tracker = EventTracker()
    cam = "camBadStart2"
    add = {"item": {"id": "e1", "device": cam, "start": "not-an-int",
                     "smartDetectTypes": ["person"]}}

    assert tracker.handle(add) == {cam}
    assert tracker.is_active(cam) is True
    assert tracker.last_motion_ms(cam) is None


# ---------------------------------------------------------------------
# BUG 3: a bad element type in smartDetectTypes must never discard the
# frame carrying it — especially not one carrying `end`.
# ---------------------------------------------------------------------

def test_missing_smart_detect_types_degrades_to_empty_set_not_discarded():
    """A missing smartDetectTypes key must not discard the frame — the
    add/end lifecycle signal still applies, just with an empty detect-type
    union."""
    tracker = EventTracker()
    add = {"type": "add", "item": {"id": "e1", "device": "camZ"}}

    assert tracker.handle(add) == {"camZ"}
    assert tracker.is_active("camZ") is True
    assert tracker.detect_types("camZ") == set()


def test_non_list_smart_detect_types_degrades_to_empty_set():
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "camZ2",
                     "smartDetectTypes": "person"}}  # a str, not a list

    assert tracker.handle(add) == {"camZ2"}
    assert tracker.detect_types("camZ2") == set()


def test_unhashable_smart_detect_type_element_does_not_discard_end():
    """Reproduced: smartDetectTypes: [{"type": "person"}] -> set() of an
    unhashable dict -> TypeError. If the frame carrying that raises the
    error also carries `end`, the camera must still clear rather than
    latch on forever.
    """
    tracker = EventTracker()
    cam = "camBadTypes"
    add = {"item": {"id": "e1", "device": cam, "start": 100,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}

    end = {"item": {"id": "e1", "device": cam, "start": 100, "end": 200,
                     "smartDetectTypes": [{"type": "person"}]}}
    changed = tracker.handle(end)

    assert changed == {cam}
    assert tracker.is_active(cam) is False
    assert tracker.detect_types(cam) == set()
    # Tolerated, not discarded — no diagnostic counter should fire.
    assert tracker.malformed_count == 0
    assert tracker.dropped_terminal_count == 0


# ---------------------------------------------------------------------
# BUG 4: the swallow needs a voice — malformed_count / dropped_terminal_count
# ---------------------------------------------------------------------

def test_dropped_terminal_count_rises_when_terminal_frame_is_destroyed():
    """A frame that is genuinely unparseable (missing `device`, here) but
    carries `end` is a lost lifecycle signal, not a no-op — it must be
    counted, distinctly from an ordinary malformed frame."""
    tracker = EventTracker()
    assert tracker.malformed_count == 0
    assert tracker.dropped_terminal_count == 0

    bad_end = {"item": {"id": "e1", "end": 200,
                         "smartDetectTypes": ["person"]}}  # no 'device'
    changed = tracker.handle(bad_end)

    assert changed == set()
    assert tracker.malformed_count == 1
    assert tracker.dropped_terminal_count == 1


def test_malformed_non_terminal_frame_counts_as_malformed_not_dropped_terminal():
    """A malformed frame with no `end` is still counted as malformed (a
    frame was destroyed), but must NOT inflate dropped_terminal_count —
    that counter is specifically for lost lifecycle signals."""
    tracker = EventTracker()
    bad_add = {"item": {"id": "e1",
                         "smartDetectTypes": ["person"]}}  # no 'device'

    changed = tracker.handle(bad_add)

    assert changed == set()
    assert tracker.malformed_count == 1
    assert tracker.dropped_terminal_count == 0


def test_duplicate_frame_does_not_move_malformed_counters():
    """A duplicate/stale frame is a legitimate no-op, not a destroyed
    frame — it must not move either diagnostic counter."""
    tracker = EventTracker()
    cam = "camDup"
    add = {"item": {"id": "e1", "device": cam, "start": 1,
                     "smartDetectTypes": ["person"]}}
    end = {"item": {"id": "e1", "device": cam, "start": 1, "end": 2,
                     "smartDetectTypes": ["person"]}}
    tracker.handle(add)
    tracker.handle(end)

    assert tracker.handle(end) == set()  # duplicate end
    assert tracker.malformed_count == 0
    assert tracker.dropped_terminal_count == 0


class _ExplodingItem(dict):
    """A dict that raises when a specific key is queried, used to drive a
    genuine, unanticipated exception into EventTracker.handle()'s blanket
    except — proving that except is reachable (and counted), not dead code."""

    def get(self, key, default=None):
        if key == "device":
            raise RuntimeError("boom")
        return super().get(key, default)


def test_blanket_except_is_reachable_and_counted():
    """Right now the blanket `except Exception` in handle() is dead code —
    every input in this suite is handled by the explicit validation paths.
    This test drives a real, unanticipated exception into it directly (via
    a collaborator that raises on attribute access, not via any input
    shape _parse_item already validates) and asserts the counters move,
    proving the except is reachable and does its job rather than being
    untested dead code.
    """
    tracker = EventTracker()
    item = _ExplodingItem(id="e1", device="camX", end=999,
                           smartDetectTypes=["person"])
    message = {"type": "update", "item": item}

    assert tracker.malformed_count == 0
    assert tracker.dropped_terminal_count == 0

    changed = tracker.handle(message)  # must not raise

    assert changed == set()
    assert tracker.malformed_count == 1
    assert tracker.dropped_terminal_count == 1


def test_counters_are_read_only_properties():
    tracker = EventTracker()
    with pytest.raises(AttributeError):
        tracker.malformed_count = 5
    with pytest.raises(AttributeError):
        tracker.dropped_terminal_count = 5
