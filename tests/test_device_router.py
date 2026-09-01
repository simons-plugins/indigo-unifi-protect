"""Tests for device_router.py (issue #18).

Mirrors test_event_tracker.py's stance: pure logic, no Indigo imports, no
network -- every test asks "when could this route/merge and be WRONG?"
rather than just confirming the happy path.
"""

import json
from pathlib import Path

import pytest

from device_router import (
    HANDLED_MODEL_KEYS,
    MAX_IGNORED_MODEL_KEYS,
    MISSING_MODEL_KEY,
    OTHER_MODEL_KEY,
    DeviceUpdateRouter,
    merge_update,
)

CAPTURE_PATH = Path(__file__).parent / "fixtures" / "ws_devices_capture.json"

# ---------------------------------------------------------------------
# route(): malformed shapes -> None, counted in malformed_count, never raise
# ---------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    {},
    None,
    "not a dict",
    123,
    [1, 2, 3],
    {"type": "update"},                                   # missing item
    {"type": "update", "item": "not a dict"},              # item not a dict
    {"type": "update", "item": {"modelKey": "camera"}},    # missing id
    {"type": "update", "item": {"id": "", "modelKey": "camera"}},   # empty id
    {"type": "update", "item": {"id": 42, "modelKey": "camera"}},   # non-string id
    {"item": {"id": "x", "modelKey": "camera"}},           # missing type
    {"type": "sync", "item": {"id": "x", "modelKey": "camera"}},    # unknown type
    {"type": None, "item": {"id": "x", "modelKey": "camera"}},      # non-string type
])
def test_route_malformed_shapes_return_none_and_are_counted(message):
    router = DeviceUpdateRouter()
    assert router.route(message) is None
    assert router.malformed_count == 1
    assert router.ignored_model_counts == {}


def test_route_never_raises_on_deeply_wrong_input():
    router = DeviceUpdateRouter()
    for bad in (object(), {"type": {}}, {"type": "add", "item": {"id": ["x"]}}):
        assert router.route(bad) is None


# ---------------------------------------------------------------------
# route(): unhandled modelKey -> None, counted in ignored_model_counts,
# NOT malformed_count
# ---------------------------------------------------------------------

@pytest.mark.parametrize("model_key", ["bridge", "speaker", "aiprocessor",
                                        "aiport", "linkstation", "somethingBrandNew"])
def test_route_unhandled_model_key_is_ignored_not_malformed(model_key):
    router = DeviceUpdateRouter()
    message = {"type": "update", "item": {"id": "dev-1", "modelKey": model_key}}
    assert router.route(message) is None
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {model_key: 1}


def test_route_missing_model_key_is_ignored_under_missing_key():
    router = DeviceUpdateRouter()
    message = {"type": "add", "item": {"id": "dev-1"}}
    assert router.route(message) is None
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {MISSING_MODEL_KEY: 1}


def test_route_non_string_model_key_is_ignored_under_missing_key():
    router = DeviceUpdateRouter()
    message = {"type": "add", "item": {"id": "dev-1", "modelKey": 7}}
    assert router.route(message) is None
    assert router.ignored_model_counts == {MISSING_MODEL_KEY: 1}


def test_ignored_model_counts_accumulate_per_key():
    router = DeviceUpdateRouter()
    router.route({"type": "update", "item": {"id": "a", "modelKey": "speaker"}})
    router.route({"type": "update", "item": {"id": "b", "modelKey": "speaker"}})
    router.route({"type": "update", "item": {"id": "c", "modelKey": "bridge"}})
    assert router.ignored_model_counts == {"speaker": 2, "bridge": 1}


def test_ignored_model_counts_is_a_copy_not_live_state():
    router = DeviceUpdateRouter()
    router.route({"type": "update", "item": {"id": "a", "modelKey": "speaker"}})
    counts = router.ignored_model_counts
    counts["speaker"] = 999
    assert router.ignored_model_counts == {"speaker": 1}


# ---------------------------------------------------------------------
# route(): valid add/update/remove for each handled modelKey
# ---------------------------------------------------------------------

@pytest.mark.parametrize("model_key", sorted(HANDLED_MODEL_KEYS))
@pytest.mark.parametrize("frame_type", ["add", "update", "remove"])
def test_route_handled_model_key_returns_correct_tuple(frame_type, model_key):
    router = DeviceUpdateRouter()
    item = {"id": "dev-1", "modelKey": model_key, "extra": "field"}
    message = {"type": frame_type, "item": item}
    result = router.route(message)
    assert result == (frame_type, model_key, "dev-1", item)
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {}


def test_handled_model_keys_are_exactly_the_six_indigo_device_types():
    assert HANDLED_MODEL_KEYS == {"camera", "sensor", "light", "chime", "nvr", "viewer"}


def test_route_viewer_update_frame_routes_not_ignored():
    """Issue #22: viewer moved from ignored_model_counts into
    HANDLED_MODEL_KEYS -- a viewer update frame must now route like any
    other handled class, not fall into the ignore-and-count path."""
    router = DeviceUpdateRouter()
    item = {"id": "viewer-1", "modelKey": "viewer", "state": "CONNECTED"}
    result = router.route({"type": "update", "item": item})
    assert result == ("update", "viewer", "viewer-1", item)
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {}


# ---------------------------------------------------------------------
# merge_update()
# ---------------------------------------------------------------------

def test_merge_update_overwrites_top_level_keys():
    cached = {"id": "cam-1", "name": "Old Name", "state": "CONNECTED"}
    item = {"name": "New Name"}
    merged = merge_update(cached, item)
    assert merged == {"id": "cam-1", "name": "New Name", "state": "CONNECTED"}


def test_merge_update_replaces_nested_dict_wholesale_not_deep_merged():
    """THE verified wire semantic (docs/API-REFERENCE.md): a nested object
    arrives WHOLE on any change within it, never diffed to the one field
    that moved. A key present only in the cached nested dict must be GONE
    after the merge -- proving this is a replace, not update()/deep-merge."""
    cached = {
        "id": "cam-1",
        "ledSettings": {"isEnabled": False, "welcomeLed": True, "floodLed": True},
    }
    item = {"ledSettings": {"isEnabled": True}}   # as if only isEnabled were sent
    merged = merge_update(cached, item)
    assert merged["ledSettings"] == {"isEnabled": True}
    assert "welcomeLed" not in merged["ledSettings"], (
        "a nested object must be replaced wholesale, not deep-merged -- "
        "keeping welcomeLed here would fabricate a value the frame never sent"
    )


def test_merge_update_does_not_mutate_cached_or_item():
    cached = {"id": "cam-1", "ledSettings": {"isEnabled": False}}
    item = {"ledSettings": {"isEnabled": True}}
    cached_copy = {"id": "cam-1", "ledSettings": {"isEnabled": False}}
    item_copy = {"ledSettings": {"isEnabled": True}}

    merge_update(cached, item)

    assert cached == cached_copy
    assert item == item_copy


def test_merge_update_returns_new_dict_object():
    cached = {"id": "cam-1"}
    merged = merge_update(cached, {"name": "x"})
    assert merged is not cached


def test_merge_update_non_dict_item_returns_unchanged_copy():
    cached = {"id": "cam-1", "name": "x"}
    merged = merge_update(cached, "not a dict")
    assert merged == cached
    assert merged is not cached


def test_merge_update_non_dict_item_none_returns_unchanged_copy():
    cached = {"id": "cam-1"}
    assert merge_update(cached, None) == cached


def test_merge_update_non_dict_cached_degrades_to_item_only():
    merged = merge_update("not a dict", {"id": "cam-1", "name": "x"})
    assert merged == {"id": "cam-1", "name": "x"}


def test_merge_update_never_raises_on_pathological_input():
    for cached, item in [(None, None), (123, {}), ({}, 123), ([], [])]:
        merge_update(cached, item)   # must not raise


# ---------------------------------------------------------------------
# Real-shaped capture (tests/fixtures/ws_devices_capture.json) -- see
# tests/fixtures/README.md for provenance: the two `update` frames mirror
# the live 2026-08-31 capture in docs/API-REFERENCE.md, the `add`/`remove`
# are spec-derived.
# ---------------------------------------------------------------------

def _load_capture():
    with open(CAPTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_capture_camera_update_routes_and_is_a_whole_nested_object():
    frames = _load_capture()
    router = DeviceUpdateRouter()
    result = router.route(frames[0])
    assert result == ("update", "camera", "69be54f600574703e4000ff4", frames[0]["item"])
    # The verified wire fact this capture pins: ledSettings arrives WHOLE.
    assert set(result[3]["ledSettings"]) == {"isEnabled", "welcomeLed", "floodLed"}


def test_capture_bridge_update_is_ignored_not_malformed():
    frames = _load_capture()
    router = DeviceUpdateRouter()
    assert router.route(frames[1]) is None
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {"bridge": 1}


def test_capture_sensor_add_routes_as_full_object():
    frames = _load_capture()
    router = DeviceUpdateRouter()
    result = router.route(frames[2])
    assert result[:3] == ("add", "sensor", "6a2b1d8f00bb4703e400aa22")


def test_capture_camera_remove_routes_as_bare_reference():
    frames = _load_capture()
    router = DeviceUpdateRouter()
    result = router.route(frames[3])
    assert result == ("remove", "camera", "69be54f600574703e4000ff4",
                       {"id": "69be54f600574703e4000ff4", "modelKey": "camera"})


def test_capture_all_four_frames_replay_with_expected_tallies():
    frames = _load_capture()
    router = DeviceUpdateRouter()
    results = [router.route(f) for f in frames]
    assert [r is not None for r in results] == [True, False, True, True]
    assert router.malformed_count == 0
    assert router.ignored_model_counts == {"bridge": 1}


# ---------------------------------------------------------------------
# T12 (F9): ignored_model_counts is capped at MAX_IGNORED_MODEL_KEYS
# distinct keys, mirroring event_tracker's own ignored_type_counts cap --
# see test_event_tracker.py::test_ignored_type_counts_caps_distinct_keys_and_buckets_overflow.
# ---------------------------------------------------------------------

def test_ignored_model_counts_caps_distinct_keys_and_buckets_overflow():
    router = DeviceUpdateRouter()
    for i in range(MAX_IGNORED_MODEL_KEYS):
        router.route({"type": "update", "item": {"id": f"d{i}", "modelKey": f"weirdModel{i}"}})

    assert len(router.ignored_model_counts) == MAX_IGNORED_MODEL_KEYS

    # The next two distinct modelKeys must be bucketed under OTHER_MODEL_KEY,
    # not get their own keys.
    router.route({"type": "update", "item": {"id": "over1", "modelKey": "weirdModelOverflow1"}})
    router.route({"type": "update", "item": {"id": "over2", "modelKey": "weirdModelOverflow2"}})

    assert len(router.ignored_model_counts) == MAX_IGNORED_MODEL_KEYS + 1
    assert "weirdModelOverflow1" not in router.ignored_model_counts
    assert "weirdModelOverflow2" not in router.ignored_model_counts
    assert router.ignored_model_counts[OTHER_MODEL_KEY] == 2
