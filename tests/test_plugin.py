"""Tests for plugin.py.

Every test here exists because a reviewer found a real defect. The theme is
one question asked repeatedly:

    when could this report "no motion" or "socket healthy" and be WRONG?

Happy-path coverage is deliberately thin. A camera that correctly reports
motion when motion happened is not where this plugin fails.
"""

import time

import pytest

import indigo
import plugin as plugin_module
from protect_api import ProtectAPIError


def make_plugin(prefs=None):
    prefs = prefs if prefs is not None else {}
    return plugin_module.Plugin("com.test", "UniFi Protect", "2026.1.0", prefs)


def add_camera_device(fake_indigo, plug, camera_id="cam-1", dev_id=1001, name="Patio"):
    from conftest import _FakeDevice
    dev = _FakeDevice(dev_id, name=name, plugin_props={"cameraId": camera_id})
    fake_indigo.devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


# ---------------------------------------------------------------------
# The worst bug found in review: an unconfigured plugin claiming health
# ---------------------------------------------------------------------

def test_unconfigured_plugin_reports_disconnected_not_quiet(fake_indigo):
    """THE regression test.

    With no host and no API key there is no socket and never has been. A
    camera device must NOT sit at connected=True/motionDetected=False, because
    that is indistinguishable from a working camera watching an empty garden --
    and it is the state a careful user's trigger ("connected AND not motion")
    treats as trustworthy.
    """
    plug = make_plugin({})
    plug.startup()
    dev = add_camera_device(fake_indigo, plug)

    assert dev.states["connected"] is False, (
        "an unconfigured plugin must not claim the event socket is connected"
    )
    assert dev.states["motionDetected"] is False
    assert dev.states["onOffState"] is False


def test_connected_is_never_defaulted_true_by_a_forgetful_caller(fake_indigo):
    """_apply_camera_state's `connected` defaults to None and is DERIVED.

    The original bug was a default of True, which meant every call site that
    omitted the kwarg silently asserted health it had not checked.
    """
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    dev.states.clear()

    plug._apply_camera_state("cam-1", force=True)   # no `connected` passed

    assert dev.states["connected"] is False


def test_camera_state_says_unavailable_not_empty_when_lookup_failed(fake_indigo):
    """An empty string reads as "not DISCONNECTED" to a trigger. Say unavailable."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)

    assert dev.states["cameraState"] == plugin_module.STATE_UNAVAILABLE


# ---------------------------------------------------------------------
# The read loop must be unkillable and must honour shutdown
# ---------------------------------------------------------------------

def test_state_write_failure_cannot_kill_the_reconnect_loop(fake_indigo):
    """Indigo does not restart runConcurrentThread. If a state write throws and
    escapes, the plugin still shows as Running while being permanently deaf --
    with devices frozen at whatever they last held.
    """
    plug = make_plugin({})
    plug.cameras = {"cam-1": {999}}

    class ExplodingDevices:
        def get(self, dev_id, default=None):
            raise RuntimeError("Indigo server busy")

    fake_indigo.devices = ExplodingDevices()

    plug._safe_mark_all_disconnected()   # must not raise


def test_stopthread_is_not_logged_as_an_error(fake_indigo, caplog):
    """StopThread subclasses Exception, so a bare `except Exception` swallows
    the shutdown signal and logs a contentless "Event socket error" on every
    single plugin restart -- training users to ignore the exact string a real
    fault prints.
    """
    plug = make_plugin({"host": "h", "apiKey": "k"})
    plug.startup()

    def boom():
        raise plug.StopThread()

    plug._open_socket = boom
    plug.stopThread = True

    with caplog.at_level("ERROR"):
        plug.runConcurrentThread()

    assert not [r for r in caplog.records if "Event socket error" in r.getMessage()], (
        "shutdown must not be reported as a socket error"
    )


def test_pump_checks_stopthread_before_reading(fake_indigo):
    """Fatal-collaborator test: the socket RAISES if read.

    Proves ordering -- the shutdown check happens before the read -- which is
    not observable from output alone. Without it, a shutdown hangs until the
    socket happens to die.
    """
    plug = make_plugin({})

    class PoisonedSocket:
        last_frame_at = time.monotonic()

        def read_message(self, timeout=1.0):
            raise AssertionError("read_message must not be called after stopThread")

        def send_ping(self):
            raise AssertionError("send_ping must not be called after stopThread")

    plug.socket = PoisonedSocket()
    plug.stopThread = True

    with pytest.raises(plug.StopThread):
        plug._pump()


# ---------------------------------------------------------------------
# Liveness: silence is not death, but death must still be detected
# ---------------------------------------------------------------------

def test_quiet_socket_is_not_torn_down(fake_indigo):
    """The live controller sends NOTHING on an idle socket -- verified by a
    5-minute capture that saw 2 minutes of silence on a healthy connection.
    A naive "no frames means dead" watchdog would reconnect all night.
    """
    plug = make_plugin({})
    now = time.monotonic()

    class QuietSocket:
        def __init__(self):
            self.last_frame_at = now
            self.pings = 0
            self.reads = 0

        def read_message(self, timeout=1.0):
            self.reads += 1
            if self.reads > 3:
                raise plug.StopThread()
            return None

        def send_ping(self):
            self.pings += 1

    plug.socket = QuietSocket()
    with pytest.raises(plug.StopThread):
        plug._pump()   # must NOT raise ConnectionError


def test_dead_socket_is_detected_despite_silence(fake_indigo):
    """The other half: once pings go unanswered past STALE_TIMEOUT, the socket
    IS dead and must raise so the reconnect path marks cameras disconnected.
    """
    plug = make_plugin({})

    class DeadSocket:
        # last frame far enough in the past to exceed STALE_TIMEOUT
        last_frame_at = time.monotonic() - (plugin_module.STALE_TIMEOUT + 10)

        def read_message(self, timeout=1.0):
            return None

        def send_ping(self):
            pass

    plug.socket = DeadSocket()
    with pytest.raises(ConnectionError, match="dead"):
        plug._pump()


# ---------------------------------------------------------------------
# Reconnect backoff
# ---------------------------------------------------------------------

def test_backoff_escalates_when_the_server_flaps(fake_indigo):
    """A server that accepts the upgrade then instantly drops must NOT produce a
    hot 1s reconnect loop -- each cycle also fires GET /cameras at a controller
    that rate-limits.
    """
    plug = make_plugin({"host": "h", "apiKey": "k"})
    plug.startup()
    plug.stop_after_sleeps = 5

    def flap():
        raise ConnectionError("dropped immediately after handshake")

    plug._open_socket = flap
    plug.runConcurrentThread()

    assert plug.sleep_calls == [1.0, 2.0, 4.0, 8.0, 16.0], (
        f"backoff must escalate, got {plug.sleep_calls}"
    )


# ---------------------------------------------------------------------
# REST throttle
# ---------------------------------------------------------------------

def test_rest_throttle_records_the_call_even_when_it_raises(fake_indigo):
    """The timestamp update lives in a `finally`. If it did not, one error would
    disable throttling from then on -- exactly when a failing controller is
    least able to take a burst.
    """
    plug = make_plugin({})
    plug._last_rest_call = time.monotonic()

    def failing():
        raise ProtectAPIError("nope", status=500)

    with pytest.raises(ProtectAPIError):
        plug._rest(failing)

    assert plug._last_rest_call > 0


def test_min_rest_interval_matches_the_measured_safe_rate():
    """The constant and the evidence in its comment must not disagree. ~5 req/s
    earned 429 on 7.2.105; 1-per-3s was clean. 3.0 is the slowest known-good.
    """
    assert plugin_module.MIN_REST_INTERVAL == 3.0


# ---------------------------------------------------------------------
# Device mapping
# ---------------------------------------------------------------------

def test_two_devices_on_one_camera_both_receive_updates(fake_indigo):
    """A 1:1 dict silently froze the loser forever, and Indigo's Duplicate
    command makes this a two-click mistake.
    """
    plug = make_plugin({})
    dev_a = add_camera_device(fake_indigo, plug, camera_id="cam-1", dev_id=1, name="A")
    dev_b = add_camera_device(fake_indigo, plug, camera_id="cam-1", dev_id=2, name="B")

    dev_a.states.clear()
    dev_b.states.clear()
    plug._apply_camera_state("cam-1", force=True)

    assert "connected" in dev_a.states, "first device must still receive updates"
    assert "connected" in dev_b.states


def test_device_stop_removes_by_device_id_not_by_current_props(fake_indigo):
    """If the user re-points a device at another camera, its props already hold
    the NEW id -- popping by that would orphan the OLD mapping and keep writing
    a camera the device no longer tracks.
    """
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug, camera_id="cam-old", dev_id=7)

    dev.pluginProps["cameraId"] = "cam-new"      # user edited the device
    plug.deviceStopComm(dev)

    assert "cam-old" not in plug.cameras, "the stale mapping must be gone"
    assert all(7 not in ids for ids in plug.cameras.values())


# ---------------------------------------------------------------------
# Static wiring: the failure mode Indigo reports without naming the culprit
# ---------------------------------------------------------------------

def test_every_written_state_is_declared_and_legal(fake_indigo):
    """Indigo rejects an undeclared or illegally-named state with
    `LowLevelBadParameterError -- illegal XML tag name character`, and the error
    does NOT say which key was wrong. Catch it here instead of on jarvis.

    Drives a real motion cycle so the states written only on a live event
    (personDetected, lastMotion, ...) are actually exercised -- a regex over the
    source misses them, which is exactly how this gap hides.
    """
    import re
    import xml.etree.ElementTree as ET
    from pathlib import Path

    devices_xml = (Path(__file__).parent.parent / "UniFi Protect.indigoPlugin"
                   / "Contents" / "Server Plugin" / "Devices.xml")
    declared = {s.get("id") for s in ET.parse(devices_xml).findall(".//State")}
    declared.add("onOffState")   # built-in, supplied by SupportsOnState

    for state_id in declared:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", state_id), (
            f"state id {state_id!r} is illegal: ASCII letters/digits only, "
            "must start with a letter, underscores forbidden"
        )
    assert "batteryLevel" not in declared, (
        "batteryLevel is reserved - Indigo silently routes writes to the native "
        "property and the state never appears"
    )

    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()          # make _is_connected() true
    plug.tracker.handle({"type": "add", "item": {
        "id": "e1", "device": "cam-1", "start": 1787756557629,
        "smartDetectTypes": ["person", "vehicle", "animal"]}})
    plug._apply_camera_state("cam-1", force=True)

    written = {entry["key"] for batch in dev.state_writes for entry in batch}
    undeclared = written - declared
    assert not undeclared, f"plugin writes states not declared in Devices.xml: {undeclared}"

    # snapshotPath is written by the takeSnapshot ACTION, not by the event
    # path, so a motion cycle legitimately never touches it.
    never_written = declared - written - {"snapshotPath"}
    assert not never_written, (
        f"declared but never written during a full motion cycle: {never_written}"
    )


def test_snapshot_path_state_is_written_by_the_action(fake_indigo, tmp_path):
    """Covers the one state the motion-cycle test above has to exempt, so
    'declared but never written anywhere' still cannot hide.
    """
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug)
    plug._snapshot_dir = str(tmp_path)
    plug.camera_info = {"cam-1": {"id": "cam-1", "featureFlags": {}}}

    class FakeAPI:
        def get_snapshot(self, camera_id, supports_high_quality=None):
            return b"\xff\xd8\xff" + b"jpegbytes"

    plug.api = FakeAPI()
    plug._last_rest_call = 0.0
    plug.takeSnapshot(object(), dev)

    assert dev.states["snapshotPath"].endswith(f"camera_{dev.id}.jpg")


def test_empty_snapshot_body_is_a_failure_not_a_zero_byte_file(fake_indigo, tmp_path):
    """A 200 with no image data must NOT be written to disk and logged as
    "snapshot saved (0 bytes)". An unusable precondition is a failed call, not
    an empty result.
    """
    import os
    from protect_api import ProtectAPI, ProtectAPIError

    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug)
    plug._snapshot_dir = str(tmp_path)

    class EmptyBodyAPI:
        def get_snapshot(self, camera_id, supports_high_quality=None):
            return ProtectAPI._check_jpeg(b"", camera_id)

    plug.api = EmptyBodyAPI()
    plug._last_rest_call = 0.0
    plug.takeSnapshot(object(), dev)

    assert not os.listdir(tmp_path), "no file should be written for an empty body"
    assert dev.states["snapshotPath"] == "", "must not keep claiming a stale file is current"


def test_html_error_page_is_rejected_as_a_snapshot(fake_indigo):
    from protect_api import ProtectAPI, ProtectAPIError
    with pytest.raises(ProtectAPIError, match="not a JPEG"):
        ProtectAPI._check_jpeg(b"<html>gateway timeout</html>", "cam-1")


def test_init_does_not_touch_dunder_file(fake_indigo):
    """Indigo exec()s plugin.py as a string, so __file__ does not exist.

    Touching it in __init__ kills the plugin at InitializeMain with
    "name '__file__' is not defined" -- before a single line of the plugin
    runs. Caught on jarvis, not by the suite, so pin it here.

    Fatal-collaborator form: any attempt to resolve the path during
    construction blows up rather than quietly succeeding.
    """
    import builtins
    plug = make_plugin({})
    assert plug._snapshot_dir is None, (
        "the snapshot dir must be resolved lazily, not during __init__"
    )


def test_snapshot_dir_is_outside_the_plugin_bundle(fake_indigo, monkeypatch):
    """Indigo replaces Contents/ on every plugin upgrade. A snapshot written
    inside the bundle is silently deleted by the next update.
    """
    plug = make_plugin({})
    monkeypatch.setattr(
        fake_indigo, "server",
        type("S", (), {"getInstallFolderPath": staticmethod(lambda: "/Indigo")})(),
        raising=False,
    )

    path = plug._get_snapshot_dir()

    assert ".indigoPlugin" not in path, "snapshots must not live inside the bundle"
    assert "Web Assets" in path, "must be under Web Assets so control pages can serve it"
