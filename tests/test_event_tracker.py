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
AUDIO_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ws_capture_audio.json"

CAMERA_A = "69ed1b24002f7f03e407c90a"  # event 9b5f3607-...
CAMERA_B = "69bea2c1001d4703e4002686"  # event bd8282c1-...
SIDE_PATH_CAMERA = "69be54f600574703e4000ff4"  # smartAudioDetect only


def load_capture():
    """Return the 10 real captured messages, in wire order."""
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        frames = json.load(f)
    return [f["message"] for f in frames]


def load_audio_capture():
    """Return the 13 real captured messages from ws_capture_audio.json, in
    wire order: 10 smartDetectZone frames across the same two cameras as
    ws_capture.json, interleaved with 3 smartAudioDetect frames for a third
    camera (Side Path). Captured 2026-08-26 by speaking near that camera."""
    with open(AUDIO_FIXTURE_PATH, encoding="utf-8") as f:
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
    add = {"type": "add", "item": {"id": "evt-1", "device": "camX", "type": "smartDetectZone",
                                    "start": 1000, "smartDetectTypes": ["person"]}}
    end = {"type": "update", "item": {"id": "evt-1", "device": "camX", "type": "smartDetectZone",
                                       "start": 1000, "end": 2000,
                                       "smartDetectTypes": ["person"]}}
    stale = {"type": "update", "item": {"id": "evt-1", "device": "camX", "type": "smartDetectZone",
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

    add1 = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 100,
                      "smartDetectTypes": ["person"]}}
    add2 = {"item": {"id": "e2", "device": cam, "type": "smartDetectZone", "start": 150,
                      "smartDetectTypes": ["vehicle"]}}
    end1 = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 100, "end": 200,
                      "smartDetectTypes": ["person"]}}
    end2 = {"item": {"id": "e2", "device": cam, "type": "smartDetectZone", "start": 150, "end": 250,
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
    end = {"item": {"id": "never-seen", "device": "camY", "type": "smartDetectZone", "start": 50,
                     "end": 60, "smartDetectTypes": ["person"]}}

    changed = tracker.handle(end)

    assert changed == set()
    assert tracker.is_active("camY") is False
    assert tracker.detect_types("camY") == set()
    # last_motion is still recorded — the camera WAS seen, just never active.
    assert tracker.last_motion_ms("camY") == 50

    # And the id is now finished, so a later no-end keepalive for it must
    # also be ignored (out-of-order arrival must not resurrect it either).
    late_add = {"item": {"id": "never-seen", "device": "camY", "type": "smartDetectZone", "start": 50,
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
    add = {"item": {"id": "e1", "device": "camZ", "type": "smartDetectZone", "start": 10,
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

    add = {"item": {"id": "target", "device": cam, "type": "smartDetectZone", "start": 1,
                     "smartDetectTypes": ["person"]}}
    end = {"item": {"id": "target", "device": cam, "type": "smartDetectZone", "start": 1, "end": 2,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}
    assert tracker.handle(end) == {cam}
    assert tracker.is_active(cam) is False

    # Fill up to (but not past) the cap with unrelated finished ids.
    for i in range(cap - 1):
        other_add = {"item": {"id": f"filler-{i}", "device": "camOther", "type": "smartDetectZone",
                               "start": 10 + i, "smartDetectTypes": ["person"]}}
        other_end = {"item": {"id": f"filler-{i}", "device": "camOther", "type": "smartDetectZone",
                               "start": 10 + i, "end": 11 + i,
                               "smartDetectTypes": ["person"]}}
        tracker.handle(other_add)
        tracker.handle(other_end)

    # "target" should still be within the cap -> a stale keepalive must
    # still be suppressed, not resurrect the camera.
    stale = {"item": {"id": "target", "device": cam, "type": "smartDetectZone", "start": 1,
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
        tracker.handle({"item": {"id": eid, "device": "cam", "type": "smartDetectZone", "start": i,
                                  "smartDetectTypes": ["person"]}})
        tracker.handle({"item": {"id": eid, "device": "cam", "type": "smartDetectZone", "start": i,
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
    add = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 1,
                     "smartDetectTypes": ["person"]}}
    tracker.handle(add)
    assert tracker.is_active(cam) is True

    assert tracker.clear_camera(cam) is True
    assert tracker.is_active(cam) is False
    # Calling again on an already-idle camera reports False.
    assert tracker.clear_camera(cam) is False


def test_reset_drops_all_state():
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "cam1", "type": "smartDetectZone", "start": 1,
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
    add = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 100,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}
    assert tracker.is_active(cam) is True

    end = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": "100", "end": 200,
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
    add = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": "not-an-int",
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
    add = {"type": "add", "item": {"id": "e1", "device": "camZ", "type": "smartDetectZone"}}

    assert tracker.handle(add) == {"camZ"}
    assert tracker.is_active("camZ") is True
    assert tracker.detect_types("camZ") == set()


def test_non_list_smart_detect_types_degrades_to_empty_set():
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "camZ2", "type": "smartDetectZone",
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
    add = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 100,
                     "smartDetectTypes": ["person"]}}
    assert tracker.handle(add) == {cam}

    end = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 100, "end": 200,
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

    bad_end = {"item": {"id": "e1", "type": "smartDetectZone", "end": 200,
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
    bad_add = {"item": {"id": "e1", "type": "smartDetectZone",
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
    add = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 1,
                     "smartDetectTypes": ["person"]}}
    end = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone", "start": 1, "end": 2,
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
    item = _ExplodingItem(id="e1", device="camX", type="smartDetectZone", end=999,
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


# ---------------------------------------------------------------------
# Issue #5: motion and audio are separate families, routed on item.type.
#
# Real ground truth: tests/fixtures/ws_capture_audio.json — 13 frames
# captured live (10 smartDetectZone across the same two cameras as
# ws_capture.json, interleaved with 3 smartAudioDetect frames for a third,
# "Side Path", camera). Proves the wire facts this module is written
# against: audio's item.type is smartAudioDetect; the audio `add` carries
# an EMPTY smartDetectTypes, with classification landing on the next
# `update`; audio event ids are 24-hex, not UUIDs.
# ---------------------------------------------------------------------

def test_real_audio_capture_exact_transition_sequence():
    """Replay the 13-frame capture and assert the exact transition points
    for both zone cameras (motion) AND the Side Path camera (audio) at
    every single frame — proving the two families stay completely
    independent even interleaved on one socket. This is the bug: before
    item.type was inspected at all, an audio "speech" event was folded
    into motion state.
    """
    messages = load_audio_capture()
    tracker = EventTracker()

    # (changed, A_motion, B_motion, sidepath_motion,
    #  sidepath_audio_active, sidepath_audio_types)
    expected = [
        ({CAMERA_B}, False, True, False, False, set()),                # 1: add B (motion)
        (set(), False, True, False, False, set()),                     # 2: keepalive B
        ({SIDE_PATH_CAMERA}, False, True, False, True, set()),         # 3: add audio, EMPTY types
        ({SIDE_PATH_CAMERA}, False, True, False, True, {"alrmSpeak"}),  # 4: update, classified
        ({SIDE_PATH_CAMERA}, False, True, False, False, set()),        # 5: end audio
        ({CAMERA_A}, True, True, False, False, set()),                 # 6: add A (motion)
        (set(), True, True, False, False, set()),                      # 7: keepalive A
        (set(), True, True, False, False, set()),                      # 8: keepalive B (pre-end)
        ({CAMERA_B}, True, False, False, False, set()),                # 9: end B
        (set(), True, False, False, False, set()),                     # 10: DUPLICATE end B
        ({CAMERA_A}, False, False, False, False, set()),               # 11: end A
        (set(), False, False, False, False, set()),                    # 12: DUPLICATE end A
        (set(), False, False, False, False, set()),                    # 13: DUPLICATE end A
    ]

    assert len(messages) == len(expected) == 13

    for frame_num, (message, (want_changed, want_a, want_b, want_sp_motion,
                               want_sp_audio, want_sp_audio_types)) in enumerate(
        zip(messages, expected), start=1
    ):
        changed = tracker.handle(message)
        assert changed == want_changed, f"frame {frame_num}: changed set mismatch"
        assert tracker.is_active(CAMERA_A) is want_a, f"frame {frame_num}: camera A motion"
        assert tracker.is_active(CAMERA_B) is want_b, f"frame {frame_num}: camera B motion"
        assert tracker.is_active(SIDE_PATH_CAMERA) is want_sp_motion, (
            f"frame {frame_num}: an audio event leaked into Side Path's motion state"
        )
        assert tracker.audio_active(SIDE_PATH_CAMERA) is want_sp_audio, (
            f"frame {frame_num}: Side Path audio_active mismatch"
        )
        assert tracker.audio_types(SIDE_PATH_CAMERA) == want_sp_audio_types, (
            f"frame {frame_num}: Side Path audio_types mismatch"
        )
        # Neither zone camera should ever show audio activity — motion
        # frames must never touch audio state either.
        assert tracker.audio_active(CAMERA_A) is False, f"frame {frame_num}: camera A audio"
        assert tracker.audio_active(CAMERA_B) is False, f"frame {frame_num}: camera B audio"


# ---------------------------------------------------------------------
# Unrecognized item.type: ignored and counted, never malformed, never
# folded into either family's state.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw_type,expected_key", [
    ("ring", "ring"),
    (None, "<missing>"),
])
def test_unknown_item_type_add_never_activates_either_family(raw_type, expected_key):
    tracker = EventTracker()
    cam = "camUnknownType"
    add = {"item": {"id": "e1", "device": cam, "type": raw_type, "start": 1,
                     "smartDetectTypes": []}}

    changed = tracker.handle(add)

    assert changed == set()
    assert tracker.is_active(cam) is False
    assert tracker.audio_active(cam) is False
    assert tracker.ignored_type_counts == {expected_key: 1}
    assert tracker.malformed_count == 0


def test_item_type_key_missing_entirely_is_ignored_and_counted():
    """item.type absent altogether (not merely present-and-None) must be
    treated identically to any other unrecognized value."""
    tracker = EventTracker()
    add = {"item": {"id": "e1", "device": "camNoType", "start": 1}}

    changed = tracker.handle(add)

    assert changed == set()
    assert tracker.is_active("camNoType") is False
    assert tracker.audio_active("camNoType") is False
    assert tracker.ignored_type_counts == {"<missing>": 1}
    assert tracker.malformed_count == 0


def test_unknown_item_type_end_creates_or_finishes_nothing():
    tracker = EventTracker()
    cam = "camUnknownEnd"
    end = {"item": {"id": "e1", "device": cam, "type": "ring", "start": 1,
                     "end": 2, "smartDetectTypes": []}}

    changed = tracker.handle(end)

    assert changed == set()
    assert tracker.is_active(cam) is False
    assert tracker.audio_active(cam) is False
    assert tracker.ignored_type_counts == {"ring": 1}
    assert tracker.malformed_count == 0
    assert tracker.dropped_terminal_count == 0

    # The ignored `end` must not have finished the id either — a real zone
    # add for the same id afterward must still activate normally.
    add_same_id = {"item": {"id": "e1", "device": cam, "type": "smartDetectZone",
                             "start": 1, "smartDetectTypes": ["person"]}}
    assert tracker.handle(add_same_id) == {cam}
    assert tracker.is_active(cam) is True


def test_plain_motion_event_type_activates_motion_family_with_no_types():
    """`motion` is the documented plain (non-smart) camera motion event —
    per the OpenAPI spec it carries no smartDetectTypes at all, and that
    must not be mistaken for an unrecognized type: it still activates the
    motion family, just with an empty detect-type union."""
    tracker = EventTracker()
    cam = "camPlainMotion"
    add = {"item": {"id": "e1", "device": cam, "type": "motion", "start": 1}}

    changed = tracker.handle(add)

    assert changed == {cam}
    assert tracker.is_active(cam) is True
    assert tracker.detect_types(cam) == set()
    assert tracker.ignored_type_counts == {}


# ---------------------------------------------------------------------
# Trap 2 applies identically to audio: a stale post-end keepalive must not
# re-arm audio_active.
# ---------------------------------------------------------------------

def test_stale_audio_keepalive_after_end_does_not_rearm():
    tracker = EventTracker()
    cam = "camAudioStale"
    add = {"item": {"id": "aud-1", "device": cam, "type": "smartAudioDetect",
                     "start": 100, "smartDetectTypes": []}}
    classify = {"item": {"id": "aud-1", "device": cam, "type": "smartAudioDetect",
                          "start": 100, "smartDetectTypes": ["alrmSpeak"]}}
    end = {"item": {"id": "aud-1", "device": cam, "type": "smartAudioDetect",
                     "start": 100, "end": 200, "smartDetectTypes": ["alrmSpeak"]}}
    stale = {"item": {"id": "aud-1", "device": cam, "type": "smartAudioDetect",
                       "start": 100, "smartDetectTypes": ["alrmSpeak"]}}

    assert tracker.handle(add) == {cam}
    assert tracker.handle(classify) == {cam}
    assert tracker.handle(end) == {cam}
    assert tracker.audio_active(cam) is False

    changed = tracker.handle(stale)

    assert changed == set(), "a stale post-end audio keepalive must not report a change"
    assert tracker.audio_active(cam) is False
    assert tracker.audio_types(cam) == set()


# ---------------------------------------------------------------------
# A zone event and an audio event on one camera must be fully independent.
# ---------------------------------------------------------------------

def test_zone_and_audio_events_on_same_camera_are_independent():
    tracker = EventTracker()
    cam = "camBoth"

    zone_add = {"item": {"id": "z1", "device": cam, "type": "smartDetectZone",
                          "start": 1, "smartDetectTypes": ["person"]}}
    audio_add = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                           "start": 2, "smartDetectTypes": ["alrmSpeak"]}}
    audio_end = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                           "start": 2, "end": 3, "smartDetectTypes": ["alrmSpeak"]}}

    tracker.handle(zone_add)
    tracker.handle(audio_add)
    assert tracker.is_active(cam) is True
    assert tracker.audio_active(cam) is True

    # Ending the audio event must leave motion active and untouched.
    changed = tracker.handle(audio_end)
    assert changed == {cam}
    assert tracker.is_active(cam) is True, "ending audio must not affect motion"
    assert tracker.audio_active(cam) is False

    # Bring up a second (fresh-id) audio event, then end the still-active
    # zone event: audio must remain active and untouched.
    audio_add2 = {"item": {"id": "a2", "device": cam, "type": "smartAudioDetect",
                            "start": 4, "smartDetectTypes": ["alrmBabyCry"]}}
    zone_end = {"item": {"id": "z1", "device": cam, "type": "smartDetectZone",
                          "start": 1, "end": 5, "smartDetectTypes": ["person"]}}

    tracker.handle(audio_add2)
    assert tracker.audio_active(cam) is True

    changed = tracker.handle(zone_end)
    assert changed == {cam}
    assert tracker.is_active(cam) is False, "zone event finished"
    assert tracker.audio_active(cam) is True, "ending motion must not affect audio"


# ---------------------------------------------------------------------
# clear_camera / reset clear BOTH families.
# ---------------------------------------------------------------------

def test_clear_camera_clears_both_families():
    tracker = EventTracker()
    cam = "camClearBoth"
    zone_add = {"item": {"id": "z1", "device": cam, "type": "smartDetectZone",
                          "start": 1, "smartDetectTypes": ["person"]}}
    audio_add = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                           "start": 2, "smartDetectTypes": ["alrmSpeak"]}}
    tracker.handle(zone_add)
    tracker.handle(audio_add)
    assert tracker.is_active(cam) is True
    assert tracker.audio_active(cam) is True

    assert tracker.clear_camera(cam) is True
    assert tracker.is_active(cam) is False
    assert tracker.audio_active(cam) is False

    # Already idle in both families now.
    assert tracker.clear_camera(cam) is False


def test_clear_camera_reports_true_when_only_audio_was_active():
    """'either family' means either, not both — clear_camera must report
    True even when motion was already idle and only audio was active."""
    tracker = EventTracker()
    cam = "camAudioOnly"
    audio_add = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                           "start": 1, "smartDetectTypes": []}}
    tracker.handle(audio_add)
    assert tracker.is_active(cam) is False
    assert tracker.audio_active(cam) is True

    assert tracker.clear_camera(cam) is True
    assert tracker.audio_active(cam) is False


def test_reset_drops_both_families():
    tracker = EventTracker()
    zone_add = {"item": {"id": "z1", "device": "camR", "type": "smartDetectZone",
                          "start": 1, "smartDetectTypes": ["person"]}}
    audio_add = {"item": {"id": "a1", "device": "camR", "type": "smartAudioDetect",
                           "start": 2, "smartDetectTypes": ["alrmSpeak"]}}
    tracker.handle(zone_add)
    tracker.handle(audio_add)
    assert tracker.is_active("camR") is True
    assert tracker.audio_active("camR") is True
    assert tracker.last_motion_ms("camR") == 1
    assert tracker.last_audio_ms("camR") == 2

    tracker.reset()

    assert tracker.is_active("camR") is False
    assert tracker.audio_active("camR") is False
    assert tracker.last_motion_ms("camR") is None
    assert tracker.last_audio_ms("camR") is None

    # Full wipe — both ids can start a fresh lifecycle after reset.
    assert tracker.handle(zone_add) == {"camR"}
    assert tracker.handle(audio_add) == {"camR"}


# ---------------------------------------------------------------------
# handle() must report a camera changed when only the audio-type union
# moves, mirroring the zone-family fix for concurrent detect types.
# ---------------------------------------------------------------------

def test_handle_reports_changed_when_only_audio_type_union_moves():
    """Classification landing on the update after the add does not flip
    audio_active (already True from the add), but the audio detect-type
    union DOES move (empty -> {alrmSpeak}) — the caller (plugin.py, which
    only writes states for cameras in the returned set) needs to know, or
    speechDetected never becomes True."""
    tracker = EventTracker()
    cam = "camAudioClassify"
    add = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                     "start": 1, "smartDetectTypes": []}}
    classify = {"item": {"id": "a1", "device": cam, "type": "smartAudioDetect",
                          "start": 1, "smartDetectTypes": ["alrmSpeak"]}}

    assert tracker.handle(add) == {cam}
    assert tracker.audio_active(cam) is True
    assert tracker.audio_types(cam) == set()

    changed = tracker.handle(classify)

    assert changed == {cam}, "audio_types moving must report the camera as changed"
    assert tracker.audio_active(cam) is True   # active flag itself unchanged
    assert tracker.audio_types(cam) == {"alrmSpeak"}
