"""Tests for plugin.py.

Every test here exists because a reviewer found a real defect. The theme is
one question asked repeatedly:

    when could this report "no motion" or "socket healthy" and be WRONG?

Happy-path coverage is deliberately thin. A camera that correctly reports
motion when motion happened is not where this plugin fails.
"""

import time
from types import SimpleNamespace

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


def add_camera_device_with_props(fake_indigo, plug, extra_props,
                                  camera_id="cam-1", dev_id=1001, name="Patio"):
    """Like add_camera_device, but with extra pluginProps set — e.g.
    audioCountsAsActivity."""
    from conftest import _FakeDevice
    props = {"cameraId": camera_id}
    props.update(extra_props)
    dev = _FakeDevice(dev_id, name=name, plugin_props=props)
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

def _scenario_camera(fake_indigo, plug):
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()          # make _is_connected() true
    plug.tracker.handle({"type": "add", "item": {
        "id": "e1", "device": "cam-1", "type": "smartDetectZone", "start": 1787756557629,
        "smartDetectTypes": ["person", "vehicle", "animal"]}})
    # Drive an audio event too, so the audio-family states (audioDetected,
    # the four specifics, lastAudio, lastAudioTypes) are actually exercised
    # here rather than exempted like snapshotPath below.
    plug.tracker.handle({"type": "add", "item": {
        "id": "e2", "device": "cam-1", "type": "smartAudioDetect", "start": 1787756557630,
        "smartDetectTypes": ["alrmSpeak", "alrmBabyCry", "alrmSmoke", "alrmCmonx"]}})
    # Populate camera_info too, so the issue #4 hardware/config states
    # (cameraModel, videoMode, hdrType, micEnabled, micVolume, ledEnabled,
    # osdNameEnabled, osdDateEnabled) are actually exercised here rather
    # than silently exempted the way snapshotPath is below.
    plug.camera_info = {"cam-1": {
        "type": "UVC G5 Turret Ultra", "videoMode": "default", "hdrType": "auto",
        "isMicEnabled": True, "micVolume": 100,
        "ledSettings": {"isEnabled": True},
        "osdSettings": {"isNameEnabled": True, "isDateEnabled": True},
    }}
    plug._apply_camera_state("cam-1", force=True)
    return dev, {"snapshotPath"}   # action-only, never written by the event path


def _scenario_sensor(fake_indigo, plug):
    from conftest import _FakeDevice
    dev = _FakeDevice(2001, name="Front Door", device_type_id="protectSensor",
                       plugin_props={"sensorId": "sensor-1"})
    fake_indigo.devices.add(dev)
    plug.sensors = {"sensor-1": {dev.id}}
    plug.socket = object()
    plug.tracker.handle({"item": {"id": "sm1", "device": "sensor-1",
                                   "type": "sensorMotion", "start": 1}})
    plug.tracker.handle({"item": {"id": "sl1", "device": "sensor-1", "type": "sensorWaterLeak",
                                   "start": 2, "metadata": {"sensorMountType": {"text": "leak"}}}})
    plug.tracker.handle({"item": {"id": "sa1", "device": "sensor-1", "type": "sensorAlarm",
                                   "start": 3, "metadata": {"alarmType": {"text": "smoke"}}}})
    plug.tracker.handle({"item": {"id": "st1", "device": "sensor-1", "type": "sensorTamper",
                                   "start": 4}})
    plug.sensor_info = {"sensor-1": {
        "id": "sensor-1", "state": "CONNECTED", "mountType": "door",
        "isOpened": True, "openStatusChangedAt": 5,
        "batteryStatus": {"isLow": True, "percentage": 55},
        "stats": {
            "temperature": {"value": 21.5}, "humidity": {"value": 40.0},
            "light": {"value": 100.0},
        },
    }}
    plug._apply_sensor_state("sensor-1", force=True, poll_timestamp_ms=1787756557000)
    return dev, set()


def _scenario_light(fake_indigo, plug):
    from conftest import _FakeDevice
    dev = _FakeDevice(2002, name="Floodlight", device_type_id="protectLight",
                       plugin_props={"lightId": "light-1"})
    fake_indigo.devices.add(dev)
    plug.lights = {"light-1": {dev.id}}
    plug.socket = object()
    plug.light_info = {"light-1": {
        "id": "light-1", "state": "CONNECTED", "isLightOn": True, "isDark": True,
        "isLightForceEnabled": True, "isPirMotionDetected": True,
        "lightModeSettings": {"mode": "motion"},
        "lightDeviceSettings": {"ledLevel": 3},
        "lastMotion": 1787756557000,
    }}
    plug._apply_light_state("light-1", force=True, poll_timestamp_ms=1787756558000)
    return dev, set()


def _scenario_chime(fake_indigo, plug):
    from conftest import _FakeDevice
    dev = _FakeDevice(2003, name="Front Chime", device_type_id="protectChime",
                       plugin_props={"chimeId": "chime-1"})
    fake_indigo.devices.add(dev)
    plug.chimes = {"chime-1": {dev.id}}
    plug.socket = object()
    plug.chime_info = {"chime-1": {
        "id": "chime-1", "state": "CONNECTED", "cameraIds": ["cam-1"],
        "ringSettings": [{"cameraId": "cam-1", "repeatTimes": 1,
                           "ringtoneId": "r1", "volume": 42}],
    }}
    plug._apply_chime_state("chime-1", force=True, poll_timestamp_ms=1787756559000)
    return dev, set()


def _scenario_nvr(fake_indigo, plug):
    from conftest import _FakeDevice
    dev = _FakeDevice(2004, name="UNVR", device_type_id="protectNvr", plugin_props={})
    fake_indigo.devices.add(dev)
    plug.nvrs = {"nvr": {dev.id}}
    plug.socket = object()
    plug.nvr_info = {
        "id": "nvr1", "modelKey": "nvr", "name": "UNVR", "type": "UNVRINSTANT",
        "armMode": {"status": "disabled", "armedAt": 1000, "breachDetectedAt": 2000,
                     "breachEventCount": 1},
    }
    plug._protect_version = "7.2.105"
    plug._apply_nvr_state(force=True, poll_timestamp_ms=1787756560000)
    return dev, set()


_SCENARIOS = {
    "protectCamera": _scenario_camera,
    "protectSensor": _scenario_sensor,
    "protectLight": _scenario_light,
    "protectChime": _scenario_chime,
    "protectNvr": _scenario_nvr,
}


def test_every_written_state_is_declared_and_legal(fake_indigo):
    """Indigo rejects an undeclared or illegally-named state with
    `LowLevelBadParameterError -- illegal XML tag name character`, and the error
    does NOT say which key was wrong. Catch it here instead of on jarvis.

    Iterates every <Device> in Devices.xml (issue #8), driving one
    realistic scenario per device type so the states written only on a
    live event (personDetected, lastMotion, isOpen, ...) are actually
    exercised -- a regex over the source misses them, which is exactly
    how this gap hides.
    """
    import re
    import xml.etree.ElementTree as ET
    from pathlib import Path

    devices_xml = (Path(__file__).parent.parent / "UniFi Protect.indigoPlugin"
                   / "Contents" / "Server Plugin" / "Devices.xml")
    device_elements = ET.parse(devices_xml).findall(".//Device")
    all_declared = {s.get("id") for d in device_elements for s in d.findall("./States/State")}
    all_declared.add("onOffState")   # built-in, supplied by SupportsOnState

    for state_id in all_declared:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", state_id), (
            f"state id {state_id!r} is illegal: ASCII letters/digits only, "
            "must start with a letter, underscores forbidden"
        )
    assert "batteryLevel" not in all_declared, (
        "batteryLevel is reserved - Indigo silently routes writes to the native "
        "property and the state never appears as a declared <State>"
    )
    assert set(_SCENARIOS) == {d.get("id") for d in device_elements}, (
        "every <Device> in Devices.xml needs a scenario above, or this test "
        "silently stops covering it"
    )

    # onOffState is built-in for relay devices and for sensor devices with
    # SupportsOnState -- protectChime/protectNvr are `type="custom"` with
    # neither, so they never get one.
    _HAS_ON_OFF_STATE = {"protectCamera", "protectSensor", "protectLight"}

    for device_elem in device_elements:
        type_id = device_elem.get("id")
        declared = {s.get("id") for s in device_elem.findall("./States/State")}
        if type_id in _HAS_ON_OFF_STATE:
            declared.add("onOffState")

        plug = make_plugin({})
        dev, exempt = _SCENARIOS[type_id](fake_indigo, plug)

        written = {entry["key"] for batch in dev.state_writes for entry in batch}
        # batteryLevel is protectSensor's one NATIVE-property write
        # (SupportsBatteryLevel) -- it is never a declared <State> (asserted
        # above) so it must be excluded here too, or it would show up as
        # "undeclared".
        written_states = written - {"batteryLevel"}

        undeclared = written_states - declared
        assert not undeclared, (
            f"{type_id}: plugin writes states not declared in Devices.xml: {undeclared}"
        )

        never_written = declared - written_states - exempt
        assert not never_written, (
            f"{type_id}: declared but never written by its scenario: {never_written}"
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


# ---------------------------------------------------------------------
# Issue #5: audio detection states and onOffState presence logic.
#
# The question, per workspace convention: when could this report the
# wrong onOffState, or fold something into motion/audio that shouldn't be?
# ---------------------------------------------------------------------

def _handle_audio(plug, camera_id, event_id, smart_types, start=1):
    """Drive one audio add + one classifying update through the tracker,
    the same two-frame shape the real wire uses (add carries EMPTY
    smartDetectTypes; classification lands on the following update)."""
    plug.tracker.handle({"type": "add", "item": {
        "id": event_id, "device": camera_id, "type": "smartAudioDetect",
        "start": start, "smartDetectTypes": []}})
    plug.tracker.handle({"type": "update", "item": {
        "id": event_id, "device": camera_id, "type": "smartAudioDetect",
        "start": start, "smartDetectTypes": list(smart_types)}})


def test_speech_counts_as_activity_by_default(fake_indigo):
    """Speech on a connected camera: onOffState True, motionDetected False,
    speechDetected/audioDetected True, and the state image tracks onOffState
    (not motion-only), so it must trip on speech alone."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()          # make _is_connected() true
    _handle_audio(plug, "cam-1", "a1", ["alrmSpeak"])

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["motionDetected"] is False
    assert dev.states["audioDetected"] is True
    assert dev.states["speechDetected"] is True
    assert dev.states["babyCryDetected"] is False
    assert dev.states["onOffState"] is True, "speech must count as activity by default"
    assert dev.image_writes[-1] == indigo.kStateImageSel.MotionSensorTripped


@pytest.mark.parametrize("off_value", [False, "false"])
def test_speech_with_checkbox_off_does_not_count_as_activity(fake_indigo, off_value):
    """audioCountsAsActivity=False must exclude speech from onOffState
    without suppressing speechDetected itself -- the checkbox governs the
    presence rollup, not the specific state. Indigo can hand this prop back
    as the bool False OR the string "false" -- bool("false") is True, so
    the string form is the actual regression this guards against."""
    plug = make_plugin({})
    dev = add_camera_device_with_props(
        fake_indigo, plug, {"audioCountsAsActivity": off_value})
    plug.socket = object()
    _handle_audio(plug, "cam-1", "a1", ["alrmSpeak"])

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["speechDetected"] is True
    assert dev.states["onOffState"] is False, "checkbox off must exclude speech from onOffState"


@pytest.mark.parametrize("on_value", [True, "true"])
def test_speech_counts_as_activity_when_checkbox_explicitly_on(fake_indigo, on_value):
    """Mirrors the checkbox-off test: an explicit bool True OR the string
    "true" must both still count speech as activity."""
    plug = make_plugin({})
    dev = add_camera_device_with_props(
        fake_indigo, plug, {"audioCountsAsActivity": on_value})
    plug.socket = object()
    _handle_audio(plug, "cam-1", "a1", ["alrmSpeak"])

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["onOffState"] is True


def test_checkbox_off_still_allows_motion_to_turn_on_the_device(fake_indigo):
    """The checkbox only ever excludes AUDIO from onOffState -- it must
    never become 'disable the sensor'. A person motion event with the
    checkbox off must still turn the device on."""
    plug = make_plugin({})
    dev = add_camera_device_with_props(
        fake_indigo, plug, {"audioCountsAsActivity": False})
    plug.socket = object()
    plug.tracker.handle({"type": "add", "item": {
        "id": "z1", "device": "cam-1", "type": "smartDetectZone",
        "start": 1, "smartDetectTypes": ["person"]}})

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["motionDetected"] is True
    assert dev.states["onOffState"] is True


@pytest.mark.parametrize("audio_type,state_key", [
    ("alrmSmoke", "smokeAlarmDetected"),
    ("alrmCmonx", "coAlarmDetected"),
])
def test_alarm_sounds_never_count_as_activity_even_with_checkbox_on(fake_indigo, audio_type, state_key):
    """Neither smoke nor CO alarm sounds may ever contribute to onOffState,
    checkbox or not -- folding either in would make a 'device turned on'
    trigger fire on a real alarm, burying it under a routine motion
    notification."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)   # audioCountsAsActivity defaults True
    plug.socket = object()
    _handle_audio(plug, "cam-1", "a1", [audio_type])

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states[state_key] is True
    assert dev.states["onOffState"] is False, (
        "an alarm sound must never turn the device on, checkbox or not"
    )


def test_baby_cry_sets_its_own_state_and_counts_as_activity(fake_indigo):
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()
    _handle_audio(plug, "cam-1", "a1", ["alrmBabyCry"])

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["babyCryDetected"] is True
    assert dev.states["speechDetected"] is False
    assert dev.states["onOffState"] is True


def test_mixed_smart_detect_types_does_not_crash_write_states(fake_indigo):
    """A wire frame with a non-string smartDetectTypes element must not
    escape into _write_states' sorted()/",".join() and raise -- that would
    tear the event socket down and wipe every camera's live state, the
    opposite of what trap 3 exists to prevent."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()
    plug.tracker.handle({"type": "add", "item": {
        "id": "a1", "device": "cam-1", "type": "smartAudioDetect",
        "start": 1, "smartDetectTypes": ["alrmSpeak", 3, {"x": 1}, None]}})

    plug._apply_camera_state("cam-1", force=True)   # must not raise

    assert dev.states["speechDetected"] is True


def test_unclassified_audio_is_audio_detected_but_no_specific_type(fake_indigo):
    """The add frame's smartDetectTypes is always empty -- audioDetected
    must go True immediately, before classification lands, but none of the
    four specific states can be true for a type not yet known, and it must
    not count as onOffState activity either."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()
    plug.tracker.handle({"type": "add", "item": {
        "id": "a1", "device": "cam-1", "type": "smartAudioDetect",
        "start": 1, "smartDetectTypes": []}})

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["audioDetected"] is True
    assert dev.states["speechDetected"] is False
    assert dev.states["babyCryDetected"] is False
    assert dev.states["smokeAlarmDetected"] is False
    assert dev.states["coAlarmDetected"] is False
    assert dev.states["onOffState"] is False


def test_disconnected_camera_reports_every_audio_state_false(fake_indigo):
    """Even when the tracker still holds live audio state from before the
    socket died, a disconnected camera must report every audio state
    False/empty -- the same 'unknown, not no-activity' rule that already
    applies to motionDetected."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    _handle_audio(plug, "cam-1", "a1", ["alrmSpeak"])

    plug._apply_camera_state("cam-1", connected=False, force=True)

    assert dev.states["audioDetected"] is False
    assert dev.states["speechDetected"] is False
    assert dev.states["babyCryDetected"] is False
    assert dev.states["smokeAlarmDetected"] is False
    assert dev.states["coAlarmDetected"] is False
    assert dev.states["lastAudioTypes"] == ""
    assert dev.states["onOffState"] is False


def test_pump_logs_unknown_type_once_per_type_debug_vs_warning(fake_indigo, caplog):
    """'ring' is a documented-but-unsupported type (doorbell) and must log
    at DEBUG; 'bogus' is genuinely unrecognized and must log at WARNING.
    Both must log exactly once per type no matter how many frames of that
    type arrive, not once per frame.
    """
    plug = make_plugin({})
    plug.cameras = {}

    messages = [
        {"item": {"id": "r1", "device": "camDoor", "type": "ring", "start": 1}},
        {"item": {"id": "r2", "device": "camDoor", "type": "ring", "start": 2}},
        {"item": {"id": "b1", "device": "camWeird", "type": "bogus", "start": 1}},
        {"item": {"id": "b2", "device": "camWeird", "type": "bogus", "start": 2}},
    ]

    class FeedSocket:
        last_frame_at = time.monotonic()

        def __init__(self, msgs):
            self._msgs = list(msgs)

        def read_message(self, timeout=1.0):
            if self._msgs:
                return self._msgs.pop(0)
            raise plug.StopThread()

        def send_ping(self):
            pass

    plug.socket = FeedSocket(messages)

    with caplog.at_level("DEBUG"):
        with pytest.raises(plug.StopThread):
            plug._pump()

    # Quoted, not a bare substring: "ring" is itself a substring of
    # "Ignoring", so an unquoted needle would false-match the wrong record.
    def records(level, event_type):
        needle = f"'{event_type}'"
        return [r for r in caplog.records if r.levelname == level and needle in r.getMessage()]

    assert len(records("DEBUG", "ring")) == 1, (
        "a documented-but-unsupported type must log DEBUG exactly once, not per frame"
    )
    assert len(records("WARNING", "ring")) == 0, (
        "a documented type must not trigger the unknown-type warning"
    )
    assert len(records("WARNING", "bogus")) == 1, (
        "a genuinely unrecognized type must log WARNING exactly once, not per frame"
    )
    assert len(records("DEBUG", "bogus")) == 0


# ---------------------------------------------------------------------
# Issue #4: camera hardware/config states + dev.model.
#
# The question, per workspace convention: when could this write a
# fabricated hardware value, or clobber a real one, instead of admitting
# it doesn't know?
# ---------------------------------------------------------------------

def _first_fixture_camera():
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "cameras.json"
    with open(path) as f:
        return json.load(f)[0]


def test_camera_info_states_written_from_real_fixture(fake_indigo):
    """The first camera in the live-captured fixture: all eight camera-info
    states must land with the right values and types."""
    camera = _first_fixture_camera()
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug, camera_id=camera["id"])
    plug.camera_info = {camera["id"]: camera}
    plug.socket = object()

    plug._apply_camera_state(camera["id"], force=True)

    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra"
    assert dev.states["videoMode"] == "slowShutter"
    assert dev.states["hdrType"] == "auto"
    assert dev.states["micEnabled"] is True
    assert dev.states["micVolume"] == 100
    assert isinstance(dev.states["micVolume"], int)
    assert dev.states["ledEnabled"] is False
    assert dev.states["osdNameEnabled"] is False
    assert dev.states["osdDateEnabled"] is False


def test_camera_info_states_from_hand_built_object(fake_indigo):
    """A hand-built object layered on top of what the fixture already
    covers (slowShutter video mode, LED off) to also exercise the values
    it doesn't: hdrType == "off", isMicEnabled == False, and both OSD
    flags True."""
    info = {
        "type": "UVC G5 Turret Ultra",
        "videoMode": "slowShutter",
        "hdrType": "off",
        "isMicEnabled": False,
        "micVolume": 100,
        "ledSettings": {"isEnabled": False},
        "osdSettings": {"isNameEnabled": True, "isDateEnabled": True},
    }
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": info}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["videoMode"] == "slowShutter"
    assert dev.states["ledEnabled"] is False
    assert dev.states["osdDateEnabled"] is True
    assert dev.states["osdNameEnabled"] is True
    assert dev.states["micEnabled"] is False
    assert dev.states["micVolume"] == 100


def test_camera_info_states_untouched_when_info_unavailable(fake_indigo):
    """A dead camera_info lookup must not overwrite last-known hardware
    states with a fabricated False/"" -- cameraState already says
    unavailable, and leaving these alone is the honest choice."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {
        "type": "UVC G5 Turret Ultra", "videoMode": "default", "hdrType": "auto",
        "isMicEnabled": True, "micVolume": 42,
        "ledSettings": {"isEnabled": True},
        "osdSettings": {"isNameEnabled": True, "isDateEnabled": True},
    }}
    plug.socket = object()
    plug._apply_camera_state("cam-1", force=True)
    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra"

    # The lookup goes away entirely (e.g. a failed refresh left camera_info
    # empty) and the socket also drops.
    plug.camera_info = {}
    plug._apply_camera_state("cam-1", connected=False, force=True)

    assert dev.states["cameraState"] == plugin_module.STATE_UNAVAILABLE
    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra", "last-known model must survive"
    assert dev.states["micVolume"] == 42, "last-known mic volume must survive"

    last_batch = dev.state_writes[-1]
    written_keys = {entry["key"] for entry in last_batch}
    for key in ("cameraModel", "videoMode", "hdrType", "micEnabled",
                "micVolume", "ledEnabled", "osdNameEnabled", "osdDateEnabled"):
        assert key not in written_keys, f"{key} must not be re-written when info is unavailable"


def test_camera_info_missing_nested_objects_are_skipped_not_fabricated(fake_indigo):
    """ledSettings/osdSettings absent entirely (not just missing keys inside
    them) must not raise -- and the four boolean keys they'd feed must be
    SKIPPED, not fabricated as False. An absent reading is not the same
    thing as a confirmed 'disabled'."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {"type": "UVC G5 Bullet"}}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)   # must not raise

    for key in ("micEnabled", "ledEnabled", "osdNameEnabled", "osdDateEnabled"):
        assert key not in dev.states, f"{key} must be skipped, not fabricated as False"
    assert dev.states["cameraModel"] == "UVC G5 Bullet"


def test_camera_info_non_dict_nested_objects_are_skipped_not_fatal(fake_indigo):
    """A truthy non-dict `ledSettings`/`osdSettings` (a malformed camera
    object, not merely an absent one) must not raise AttributeError out of
    _write_states -- an uncaught exception there escapes to _pump, tears
    the socket down, and reconnects into the same bad object forever: one
    malformed camera killing motion for every camera on the account."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {
        "type": "UVC G5 Turret Ultra", "isMicEnabled": True,
        "ledSettings": "off", "osdSettings": ["not", "a", "dict"],
    }}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)   # must not raise

    for key in ("ledEnabled", "osdNameEnabled", "osdDateEnabled"):
        assert key not in dev.states, f"{key} must be skipped when its parent isn't a dict"
    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra"
    assert dev.states["micEnabled"] is True, "a sibling valid key must still be written"


def test_camera_info_partial_object_after_good_read_keeps_prior_string_values(fake_indigo):
    """A partial camera object (e.g. a read that only returned some fields)
    must not blank cameraModel/videoMode/hdrType with "" -- that would
    contradict dev.model, which keeps the real value in this exact
    scenario because it is only ever updated, never cleared."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {
        "type": "UVC G5 Turret Ultra", "videoMode": "default", "hdrType": "auto",
    }}
    plug.socket = object()
    plug._apply_camera_state("cam-1", force=True)
    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra"
    assert dev.states["videoMode"] == "default"
    assert dev.states["hdrType"] == "auto"

    # A second, partial read: type/videoMode/hdrType are all missing this
    # time, but the object itself is still present (not None).
    plug.camera_info = {"cam-1": {"isMicEnabled": True}}
    plug._apply_camera_state("cam-1", force=True)

    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra", "must keep the prior value"
    assert dev.states["videoMode"] == "default", "must keep the prior value"
    assert dev.states["hdrType"] == "auto", "must keep the prior value"


def test_bad_mic_volume_is_skipped_not_defaulted(fake_indigo):
    """A non-numeric micVolume must be skipped, not coerced to a fabricated
    0 -- 0 is a real, meaningful mic volume."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {
        "type": "UVC G5 Turret Ultra", "videoMode": "default", "hdrType": "auto",
        "isMicEnabled": True, "micVolume": "not-a-number",
        "ledSettings": {"isEnabled": True},
        "osdSettings": {"isNameEnabled": False, "isDateEnabled": False},
    }}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)

    assert "micVolume" not in dev.states
    for key in ("cameraModel", "videoMode", "hdrType", "micEnabled",
                "ledEnabled", "osdNameEnabled", "osdDateEnabled"):
        assert key in dev.states, f"{key} must still be written even when micVolume is bad"


def test_mic_volume_bool_is_skipped_not_coerced_to_one(fake_indigo):
    """int(True) == 1 -- a real-looking but entirely fabricated volume. A
    bool micVolume must be skipped exactly like a non-numeric one."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {"type": "UVC G5 Turret Ultra", "micVolume": True}}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)

    assert "micVolume" not in dev.states
    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra"


def test_dev_model_set_and_replace_on_server_called_once_when_type_differs(fake_indigo):
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    assert dev.model == "Protect Camera"
    plug.camera_info = {"cam-1": {"type": "UVC G5 Turret Ultra"}}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)

    assert dev.model == "UVC G5 Turret Ultra"
    assert dev.replace_on_server_calls == 1


def test_dev_model_not_replaced_again_when_type_unchanged(fake_indigo):
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": {"type": "UVC G5 Turret Ultra"}}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)
    plug._apply_camera_state("cam-1", force=True)

    assert dev.replace_on_server_calls == 1, (
        "a second write with the same type must not re-call replaceOnServer"
    )


def test_dev_model_not_touched_when_type_missing_or_empty(fake_indigo):
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    plug.socket = object()

    plug.camera_info = {"cam-1": {"type": ""}}
    plug._apply_camera_state("cam-1", force=True)
    assert dev.model == "Protect Camera"
    assert dev.replace_on_server_calls == 0

    plug.camera_info = {"cam-1": {}}   # no 'type' key at all
    plug._apply_camera_state("cam-1", force=True)
    assert dev.model == "Protect Camera"
    assert dev.replace_on_server_calls == 0


def test_replace_on_server_failure_does_not_prevent_state_write(fake_indigo):
    """Fatal-collaborator form, strengthened: the raising replaceOnServer
    fake also ASSERTS the state write has already landed at the moment
    it's called. That pins the ordering (state write, then model update) --
    a silent reorder fails this even though "no exception escapes" alone
    would not catch it."""
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)

    def boom():
        assert dev.states.get("cameraModel") == "UVC G5 Turret Ultra", (
            "the state write must land before replaceOnServer is ever called"
        )
        raise RuntimeError("server busy")

    dev.replaceOnServer = boom
    plug.camera_info = {"cam-1": {"type": "UVC G5 Turret Ultra"}}
    plug.socket = object()

    plug._apply_camera_state("cam-1", force=True)   # must not raise

    assert dev.states["cameraModel"] == "UVC G5 Turret Ultra", "state write must still land"


def test_model_update_failure_logs_warning_once_then_debug(fake_indigo, caplog):
    """indigo.devices.get() returns a FRESH device object on every call in
    real Indigo, so a persistent replaceOnServer failure (e.g. a device
    edit dialog left open) would otherwise retry -- and log -- on every
    single frame forever, with no visible hint anything is wrong. Two
    consecutive failures on the same device must produce exactly one
    WARNING; the second is DEBUG.

    The fake's replaceOnServer also reverts dev.model on failure, mirroring
    what a fresh fetch from the real server would show: the failed call
    never persisted, so the "current" model is still the old one -- which
    is what makes the second call attempt (and fail) again at all.
    """
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug)
    original_model = dev.model

    def boom():
        dev.model = original_model
        raise RuntimeError("device edit dialog is open")

    dev.replaceOnServer = boom
    plug.camera_info = {"cam-1": {"type": "UVC G5 Turret Ultra"}}
    plug.socket = object()

    with caplog.at_level("DEBUG"):
        plug._apply_camera_state("cam-1", force=True)
        plug._apply_camera_state("cam-1", force=True)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "could not update device model" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"
              and "could not update device model" in r.getMessage()]
    assert len(warnings) == 1, "exactly one WARNING for the whole failure episode"
    assert len(debugs) == 1, "the second consecutive failure must log at DEBUG, not WARNING again"
    assert "RuntimeError" in warnings[0].getMessage()


# ---------------------------------------------------------------------
# Issue #6: camera control actions -- the plugin's first write path.
#
# The question, per workspace convention: when could a gate check let a
# request through it shouldn't, or a refused PATCH look like it worked?
# ---------------------------------------------------------------------

def _configured_plugin(fake_indigo, camera_info):
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {"cam-1": camera_info}
    plug.socket = object()
    plug._last_rest_call = 0.0
    return plug, dev


class _RaisesIfTouched:
    """Fatal-collaborator form: proves a gate check runs BEFORE any
    request, by making the request itself blow up if it is ever reached."""

    def patch_camera(self, camera_id, body):
        raise AssertionError(f"patch_camera must not be called (camera_id={camera_id!r}, "
                              f"body={body!r})")

    def get_cameras(self):
        raise AssertionError("get_cameras must not be called")

    def get_camera(self, camera_id):
        raise AssertionError(f"get_camera must not be called (camera_id={camera_id!r})")


class _RecordingAPI:
    """Records every patch_camera call and echoes the change back as the
    (full) response, matching the real API's contract of returning the
    whole camera object. get_camera (used by setStatusLed's toggle re-read)
    returns get_camera_response if given, else the same base_info -- pass a
    different object to simulate a cache that's gone stale relative to the
    controller's real current state."""

    def __init__(self, base_info, get_camera_response=None):
        self.calls = []
        self.get_camera_calls = []
        self._info = dict(base_info)
        self._get_camera_response = get_camera_response

    def patch_camera(self, camera_id, body):
        self.calls.append((camera_id, body))
        merged = dict(self._info)
        for key, value in body.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    def get_camera(self, camera_id):
        self.get_camera_calls.append(camera_id)
        response = self._get_camera_response if self._get_camera_response is not None             else self._info
        return dict(response)


def test_set_status_led_gate_failure_does_not_touch_api(fake_indigo, caplog):
    plug, dev = _configured_plugin(fake_indigo, {
        "featureFlags": {"hasLedStatus": False}, "ledSettings": {"isEnabled": True}})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "on"}), dev)

    assert any("hasLedStatus" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_set_status_led_success_updates_camera_info_and_state(fake_indigo):
    base = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": False}}
    plug, dev = _configured_plugin(fake_indigo, base)
    api = _RecordingAPI(base)
    plug.api = api

    plug.setStatusLed(SimpleNamespace(props={"mode": "on"}), dev)

    assert api.calls == [("cam-1", {"ledSettings": {"isEnabled": True}})]
    assert plug.camera_info["cam-1"]["ledSettings"]["isEnabled"] is True
    assert dev.states["ledEnabled"] is True


def test_set_status_led_toggle_inverts_cached_value(fake_indigo):
    """Toggle re-reads the camera before inverting -- the cache can be days
    stale (last changed from the UniFi app, not this plugin). Cache says
    True; the fresh get_camera read says False; the PATCH must invert the
    FRESH value, not the stale cached one."""
    cached = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": True}}
    fresh = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": False}}
    plug, dev = _configured_plugin(fake_indigo, cached)
    api = _RecordingAPI(cached, get_camera_response=fresh)
    plug.api = api

    plug.setStatusLed(SimpleNamespace(props={"mode": "toggle"}), dev)

    assert api.get_camera_calls == ["cam-1"]
    assert api.calls == [("cam-1", {"ledSettings": {"isEnabled": True}})]


def test_set_status_led_toggle_get_camera_error_sends_no_patch(fake_indigo, caplog):
    """Fatal-collaborator form: patch_camera raises if ever reached, proving
    a failed re-read aborts the action instead of falling back to the stale
    cached value."""
    base = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": True}}
    plug, dev = _configured_plugin(fake_indigo, base)

    class RaisesOnGetCamera(_RaisesIfTouched):
        def get_camera(self, camera_id):
            raise ProtectAPIError("HTTP 500 for /cameras/cam-1", status=500)

    plug.api = RaisesOnGetCamera()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "toggle"}), dev)

    assert plug.camera_info["cam-1"] == base
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_refused_patch_leaves_camera_info_and_device_state_untouched(fake_indigo, caplog):
    """setVideoMode with a mode the camera DOES support (so the gate lets it
    through), but the controller refuses the PATCH -- proves camera_info and
    the device state are left exactly as they were, and the AJV issue text
    reaches the Event Log.
    """
    base = {"featureFlags": {"videoModes": ["default", "sport"]}, "videoMode": "default"}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug._apply_camera_state("cam-1", force=True)
    before_info = dict(plug.camera_info["cam-1"])
    before_state = dev.states.get("videoMode")

    class RefusingAPI:
        def patch_camera(self, camera_id, body):
            raise ProtectAPIError(
                "HTTP 400 for /cameras/cam-1", status=400,
                body='{"issues":[{"instancePath":"/videoMode",'
                     '"message":"must be equal to one of the allowed values"}]}')

    plug.api = RefusingAPI()

    with caplog.at_level("ERROR"):
        plug.setVideoMode(SimpleNamespace(props={"videoMode": "sport"}), dev)

    assert plug.camera_info["cam-1"] == before_info
    assert dev.states["videoMode"] == before_state
    assert any("must be equal to one of the allowed values" in r.getMessage()
               for r in caplog.records if r.levelname == "ERROR")


def test_set_video_mode_unsupported_mode_does_not_touch_api(fake_indigo, caplog):
    plug, dev = _configured_plugin(fake_indigo, {
        "featureFlags": {"videoModes": ["default", "sport"]}})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setVideoMode(SimpleNamespace(props={"videoMode": "highFps"}), dev)

    assert any("highFps" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_set_hdr_mode_gate_failure_does_not_touch_api(fake_indigo, caplog):
    plug, dev = _configured_plugin(fake_indigo, {"featureFlags": {"hasHdr": False}})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert any("hasHdr" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_set_hdr_mode_success(fake_indigo):
    base = {"featureFlags": {"hasHdr": True}, "hdrType": "auto"}
    plug, dev = _configured_plugin(fake_indigo, base)
    api = _RecordingAPI(base)
    plug.api = api

    plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert api.calls == [("cam-1", {"hdrType": "on"})]
    assert dev.states["hdrType"] == "on"


def test_set_osd_overlay_body_only_has_selected_fields(fake_indigo):
    base = {"osdSettings": {"isNameEnabled": False, "isDateEnabled": False,
                             "isLogoEnabled": True, "overlayLocation": "topLeft"}}
    plug, dev = _configured_plugin(fake_indigo, base)
    api = _RecordingAPI(base)
    plug.api = api

    plug.setOsdOverlay(SimpleNamespace(props={
        "showName": "on", "showDate": "unchanged", "showLogo": "unchanged",
        "overlayLocation": "bottomRight"}), dev)

    assert api.calls == [("cam-1", {"osdSettings": {
        "isNameEnabled": True, "overlayLocation": "bottomRight"}})]


def test_set_osd_overlay_callback_rejects_when_nothing_selected(fake_indigo, caplog):
    """A scripter calling executeAction() directly bypasses
    validateActionConfigUi entirely -- the callback must catch it too."""
    plug, dev = _configured_plugin(fake_indigo, {})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setOsdOverlay(SimpleNamespace(props={
            "showName": "unchanged", "showDate": "unchanged",
            "showLogo": "unchanged", "overlayLocation": "unchanged"}), dev)

    assert any("nothing selected" in r.getMessage().lower() for r in caplog.records
               if r.levelname == "ERROR")


def test_validate_action_config_ui_osd_overlay_all_unchanged_rejected(fake_indigo):
    plug = make_plugin({})

    valid, values, errors = plug.validateActionConfigUi(
        {"showName": "unchanged", "showDate": "unchanged",
         "showLogo": "unchanged", "overlayLocation": "unchanged"}, "setOsdOverlay", 1001)

    assert valid is False
    assert "showName" in errors and "showDate" in errors
    assert "showLogo" in errors and "overlayLocation" in errors


def test_validate_action_config_ui_osd_overlay_one_field_changed_accepted(fake_indigo):
    plug = make_plugin({})

    result = plug.validateActionConfigUi(
        {"showName": "on", "showDate": "unchanged",
         "showLogo": "unchanged", "overlayLocation": "unchanged"}, "setOsdOverlay", 1001)

    assert result[0] is True


@pytest.mark.parametrize("raw", ["150", "abc", "-1"])
def test_validate_action_config_ui_mic_volume_rejects_out_of_range(fake_indigo, raw):
    plug = make_plugin({})

    valid, _values, errors = plug.validateActionConfigUi(
        {"micVolume": raw}, "setMicVolume", 1001)

    assert valid is False
    assert "micVolume" in errors


def test_validate_action_config_ui_mic_volume_accepts_50(fake_indigo):
    plug = make_plugin({})

    result = plug.validateActionConfigUi({"micVolume": "50"}, "setMicVolume", 1001)

    assert result[0] is True


def test_set_mic_volume_50_sends_correct_body(fake_indigo):
    base = {"featureFlags": {"hasMic": True}, "micVolume": 80}
    plug, dev = _configured_plugin(fake_indigo, base)
    api = _RecordingAPI(base)
    plug.api = api

    plug.setMicVolume(SimpleNamespace(props={"micVolume": "50"}), dev)

    assert api.calls == [("cam-1", {"micVolume": 50})]
    assert dev.states["micVolume"] == 50


def test_set_mic_volume_gate_failure_does_not_touch_api(fake_indigo, caplog):
    plug, dev = _configured_plugin(fake_indigo, {"featureFlags": {"hasMic": False}})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setMicVolume(SimpleNamespace(props={"micVolume": "50"}), dev)

    assert any("hasMic" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_camera_not_cached_and_refresh_fails_no_patch_error_logged(fake_indigo, caplog):
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {}   # nothing cached yet
    plug._last_rest_call = 0.0

    class RaisesOnGetCamerasOnly(_RaisesIfTouched):
        def get_cameras(self):
            raise ProtectAPIError("HTTP 500 for /cameras", status=500)

    plug.api = RaisesOnGetCamerasOnly()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "on"}), dev)

    assert plug.camera_info == {}
    error_messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("unavailable" in m.lower() for m in error_messages)
    assert any("500" in m for m in error_messages), (
        "the HTTP 500 cause must reach an ERROR record via _describe_api_error, not "
        "just a generic 'unavailable'"
    )


def test_get_video_mode_list_known_target_returns_camera_modes(fake_indigo):
    plug = make_plugin({})
    dev = add_camera_device(fake_indigo, plug, camera_id="cam-1", dev_id=1001)
    plug.camera_info = {"cam-1": {"featureFlags": {"videoModes": ["default", "sport"]}}}

    result = plug.getVideoModeList(targetId=1001)

    assert result == [("default", "default"), ("sport", "sport")]


def test_get_video_mode_list_unknown_target_returns_spec_enum_unverified(fake_indigo):
    plug = make_plugin({})

    result = plug.getVideoModeList(targetId=0)

    assert result == [(mode, f"{mode} (unverified)")
                       for mode in plugin_module.VIDEO_MODE_SPEC_ENUM]


def test_get_video_mode_list_target_device_exists_but_camera_not_cached_falls_back(fake_indigo):
    plug = make_plugin({})
    add_camera_device(fake_indigo, plug, camera_id="cam-unknown", dev_id=1001)
    plug.camera_info = {}

    result = plug.getVideoModeList(targetId=1001)

    assert result == [(mode, f"{mode} (unverified)")
                       for mode in plugin_module.VIDEO_MODE_SPEC_ENUM]


# ---------------------------------------------------------------------
# Issue #6 review fixes: shape errors, error-kind-specific wording,
# strict enum validation, outcome logging, and a state-write safety net.
# ---------------------------------------------------------------------

def test_patch_camera_shape_error_does_not_replace_cache_and_triggers_refresh(fake_indigo, caplog):
    """patch_camera raising kind='shape' (e.g. a 200 whose body doesn't
    look like the real camera object) must NOT be cached, must not blank
    hardware states, and must trigger a real GET refresh instead."""
    base = {"id": "cam-1", "featureFlags": {"hasHdr": True}, "hdrType": "auto"}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug._apply_camera_state("cam-1", force=True)
    before_hdr_state = dev.states.get("hdrType")

    class ShapeErrorAPI:
        def __init__(self):
            self.get_cameras_calls = 0

        def patch_camera(self, camera_id, body):
            raise ProtectAPIError(
                "Unexpected response shape for /cameras/cam-1: expected the camera object",
                status=None, kind="shape", body='{"id": "cam-1"}')

        def get_cameras(self):
            self.get_cameras_calls += 1
            return [dict(base)]

    api = ShapeErrorAPI()
    plug.api = api

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert api.get_cameras_calls == 1
    assert plug.camera_info["cam-1"] == base
    assert dev.states["hdrType"] == before_hdr_state
    assert any("unusable" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_resolve_camera_refresh_succeeds_but_camera_still_absent(fake_indigo, caplog):
    """Distinct from the refresh-FAILED case: the GET succeeds, but this
    camera simply isn't in the result -- it's gone from the controller."""
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug)
    plug.camera_info = {}
    plug._last_rest_call = 0.0

    class SucceedsButMissingAPI(_RaisesIfTouched):
        def get_cameras(self):
            return [{"id": "cam-other", "featureFlags": {}}]

    plug.api = SucceedsButMissingAPI()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "on"}), dev)

    assert any("is not on the controller" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_patch_camera_401_gives_actionable_api_key_text(fake_indigo, caplog):
    base = {"featureFlags": {"hasHdr": True}}
    plug, dev = _configured_plugin(fake_indigo, base)

    class Auth401API:
        def patch_camera(self, camera_id, body):
            raise ProtectAPIError("HTTP 401 for /cameras/cam-1", status=401, kind="auth")

    plug.api = Auth401API()

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert any("Regenerate it in UniFi OS" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_patch_camera_403_adds_permission_hint(fake_indigo, caplog):
    base = {"featureFlags": {"hasHdr": True}}
    plug, dev = _configured_plugin(fake_indigo, base)

    class Auth403API:
        def patch_camera(self, camera_id, body):
            raise ProtectAPIError("HTTP 403 for /cameras/cam-1", status=403, kind="auth")

    plug.api = Auth403API()

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert any("lacks permission" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_patch_camera_transport_error_not_worded_refused_and_triggers_refresh(fake_indigo, caplog):
    base = {"id": "cam-1", "featureFlags": {"hasHdr": True}}
    plug, dev = _configured_plugin(fake_indigo, base)

    class TransportAPI:
        def __init__(self):
            self.get_cameras_calls = 0

        def patch_camera(self, camera_id, body):
            raise ProtectAPIError("Connection failure for /cameras/cam-1: timed out",
                                   status=None, kind="transport")

        def get_cameras(self):
            self.get_cameras_calls += 1
            return [dict(base)]

    api = TransportAPI()
    plug.api = api

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    error_messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert not any("refused" in m for m in error_messages)
    assert any("outcome unknown" in m for m in error_messages)
    assert api.get_cameras_calls == 1


def test_patch_camera_429_includes_retry_after_seconds(fake_indigo, caplog):
    base = {"featureFlags": {"hasHdr": True}}
    plug, dev = _configured_plugin(fake_indigo, base)

    class RateLimitedAPI:
        def patch_camera(self, camera_id, body):
            raise ProtectAPIError("HTTP 429 for /cameras/cam-1", status=429,
                                   kind="rate_limited", retry_after=7.0)

    plug.api = RateLimitedAPI()

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)

    assert any("7s" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


_ACTION_MIN_PROPS = {
    "setStatusLed": {"mode": "on"},
    "setOsdOverlay": {"showName": "on", "showDate": "unchanged",
                       "showLogo": "unchanged", "overlayLocation": "unchanged"},
    "setVideoMode": {"videoMode": "sport"},
    "setHdrMode": {"hdrType": "on"},
    "setMicVolume": {"micVolume": "50"},
}


@pytest.mark.parametrize("method_name,props", list(_ACTION_MIN_PROPS.items()))
def test_no_camera_selected_touches_nothing(fake_indigo, caplog, method_name, props):
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = add_camera_device(fake_indigo, plug, camera_id="")
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        getattr(plug, method_name)(SimpleNamespace(props=props), dev)   # must not raise

    assert plug.camera_info == {}
    assert any("no camera selected" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


@pytest.mark.parametrize("method_name,props", list(_ACTION_MIN_PROPS.items()))
def test_not_configured_touches_nothing(fake_indigo, caplog, method_name, props):
    plug = make_plugin({})   # no host/apiKey -> self.api stays None
    dev = add_camera_device(fake_indigo, plug)

    with caplog.at_level("ERROR"):
        getattr(plug, method_name)(SimpleNamespace(props=props), dev)   # must not raise

    assert plug.camera_info == {}
    assert any("not configured" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_set_status_led_invalid_mode_uppercase_rejected(fake_indigo, caplog):
    """mode='ON' must be rejected, not silently read as falsy/off and
    logged as success (the exact bug the review flagged)."""
    base = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "ON"}), dev)

    assert any("invalid mode" in r.getMessage().lower() for r in caplog.records
               if r.levelname == "ERROR")


def test_set_status_led_missing_mode_rejected(fake_indigo, caplog):
    base = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setStatusLed(SimpleNamespace(props={}), dev)

    assert any("invalid mode" in r.getMessage().lower() for r in caplog.records
               if r.levelname == "ERROR")


def test_set_osd_overlay_invalid_show_name_value_rejected(fake_indigo, caplog):
    plug, dev = _configured_plugin(fake_indigo, {})
    plug.api = _RaisesIfTouched()

    with caplog.at_level("ERROR"):
        plug.setOsdOverlay(SimpleNamespace(props={
            "showName": "yes", "showDate": "unchanged",
            "showLogo": "unchanged", "overlayLocation": "unchanged"}), dev)

    assert any("invalid showName" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_set_status_led_success_log_states_outcome(fake_indigo, caplog):
    base = {"featureFlags": {"hasLedStatus": True}, "ledSettings": {"isEnabled": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    with caplog.at_level("INFO"):
        plug.setStatusLed(SimpleNamespace(props={"mode": "off"}), dev)

    assert any("Set Status LED -> off" in r.getMessage() for r in caplog.records
               if r.levelname == "INFO")


def test_set_mic_volume_success_log_states_outcome(fake_indigo, caplog):
    base = {"featureFlags": {"hasMic": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    with caplog.at_level("INFO"):
        plug.setMicVolume(SimpleNamespace(props={"micVolume": "50"}), dev)

    assert any("Set Microphone Volume -> 50" in r.getMessage() for r in caplog.records
               if r.levelname == "INFO")


def test_set_osd_overlay_success_log_states_outcome(fake_indigo, caplog):
    base = {"osdSettings": {"isNameEnabled": False, "isDateEnabled": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    with caplog.at_level("INFO"):
        plug.setOsdOverlay(SimpleNamespace(props={
            "showName": "on", "showDate": "off",
            "showLogo": "unchanged", "overlayLocation": "unchanged"}), dev)

    assert any("Set OSD Overlay -> name on, date off" in r.getMessage() for r in caplog.records
               if r.levelname == "INFO")


def test_set_video_mode_success_log_states_outcome(fake_indigo, caplog):
    base = {"featureFlags": {"videoModes": ["default", "sport"]}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    with caplog.at_level("INFO"):
        plug.setVideoMode(SimpleNamespace(props={"videoMode": "sport"}), dev)

    assert any("Set Video Mode -> sport" in r.getMessage() for r in caplog.records
               if r.levelname == "INFO")


def test_set_hdr_mode_success_log_states_outcome(fake_indigo, caplog):
    base = {"featureFlags": {"hasHdr": True}}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    with caplog.at_level("INFO"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "auto"}), dev)

    assert any("Set HDR Mode -> auto" in r.getMessage() for r in caplog.records
               if r.levelname == "INFO")


def test_state_update_failure_after_patch_does_not_escape(fake_indigo, caplog):
    """Fatal-ish safety net: the PATCH already succeeded on the camera when
    the Indigo-side state write blows up. The exception must not escape the
    action callback, and the log must say the write applied -- NOT that the
    whole action failed, which would leave the user thinking nothing
    happened when the camera itself already changed."""
    base = {"featureFlags": {"hasHdr": True}, "hdrType": "auto"}
    plug, dev = _configured_plugin(fake_indigo, base)
    plug.api = _RecordingAPI(base)

    def boom(states):
        raise RuntimeError("server busy")

    dev.updateStatesOnServer = boom

    with caplog.at_level("ERROR"):
        plug.setHdrMode(SimpleNamespace(props={"hdrType": "on"}), dev)   # must not raise

    assert any("applied on the camera" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_validate_action_config_ui_video_mode_empty_rejected(fake_indigo):
    plug = make_plugin({})

    valid, _values, errors = plug.validateActionConfigUi({"videoMode": ""}, "setVideoMode", 1001)

    assert valid is False
    assert "videoMode" in errors


def test_validate_action_config_ui_video_mode_selected_accepted(fake_indigo):
    plug = make_plugin({})

    result = plug.validateActionConfigUi({"videoMode": "sport"}, "setVideoMode", 1001)

    assert result[0] is True
# ---------------------------------------------------------------------
# Issue #7: RTSPS stream URLs.
#
# The URL embeds an access token -- it is a credential, not just data.
# The questions here are not "does it fetch the URL?" but "can the
# checkbox-off path ever touch the network?", "can the token ever reach a
# log line, on the success path OR the error path?", and (per the
# threading review) "can REST ever block deviceStartComm/_open_socket, or
# can a stream-URL failure ever tear down the event socket?"
# ---------------------------------------------------------------------

def _make_stream_device(fake_indigo, expose_value=None, dev_id=1001, camera_id="cam-1"):
    """Builds a device WITHOUT going through deviceStartComm, so these
    tests control exactly when the stream-URL machinery runs rather than
    picking up an extra call for free."""
    from conftest import _FakeDevice
    props = {"cameraId": camera_id}
    if expose_value is not None:
        props["exposeStreamUrls"] = expose_value
    dev = _FakeDevice(dev_id, name="Side Path", plugin_props=props)
    fake_indigo.devices.add(dev)
    return dev


class _RaisingStreamAPI:
    """Fatal-collaborator: any touch of the stream endpoints proves the
    opt-out path is not actually opting out of the network. get_cameras is
    a normal, unrelated call (actionControlUniversal's RequestStatus path
    always makes it) and is allowed through."""

    def get_cameras(self):
        return []

    def get_rtsps_streams(self, camera_id):
        raise AssertionError("get_rtsps_streams must not be called when opted out")

    def create_rtsps_streams(self, camera_id, qualities):
        raise AssertionError("create_rtsps_streams must not be called when opted out")


class _FakeStreamAPI:
    """Records calls; raises if create_rtsps_streams is invoked but no
    create_response was configured, so a test asserting POST-must-not-fire
    doesn't need a separate mock."""

    def __init__(self, get_response, create_response=None):
        self._get_response = get_response
        self._create_response = create_response
        self.get_calls = []
        self.create_calls = []

    def get_rtsps_streams(self, camera_id):
        self.get_calls.append(camera_id)
        return dict(self._get_response)

    def create_rtsps_streams(self, camera_id, qualities):
        self.create_calls.append((camera_id, list(qualities)))
        if self._create_response is None:
            raise AssertionError("create_rtsps_streams should not have been called")
        return dict(self._create_response)


def _prep_pending(plug, dev, camera_id="cam-1"):
    """Wire a device straight into plug.cameras/pending, bypassing
    deviceStartComm, so pump-drain tests control exactly one device's
    lifecycle state."""
    plug.cameras.setdefault(camera_id, set()).add(dev.id)
    plug._stream_refresh_pending.add(dev.id)


@pytest.mark.parametrize("expose_value", [None, False, "false", "False"])
def test_stream_urls_opted_out_writes_empty_states_and_never_touches_api(
        fake_indigo, expose_value):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=expose_value)
    plug.api = _RaisingStreamAPI()

    plug._refresh_stream_urls(dev)   # must not raise

    assert dev.states["streamUrlHigh"] == ""
    assert dev.states["streamUrlMedium"] == ""
    assert dev.states["streamUrlLow"] == ""
    assert dev.states["streamUrlPackage"] == ""


def test_stream_urls_opting_out_clears_previously_populated_states(fake_indigo):
    """Off must ACTIVELY clear a stored token, not just stop refreshing
    it -- starts from non-empty states, unlike the test above."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=False)
    for key in plugin_module.STREAM_URL_STATES.values():
        dev.states[key] = "rtsps://x/stale"
    plug.api = _RaisingStreamAPI()

    plug._refresh_stream_urls(dev)

    for key in plugin_module.STREAM_URL_STATES.values():
        assert dev.states[key] == "", f"{key} must be cleared, not left stale"


@pytest.mark.parametrize("expose_value", [True, "true", "True"])
def test_stream_urls_opted_in_writes_from_get_response(fake_indigo, expose_value):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=expose_value)
    api = _FakeStreamAPI({
        "high": "rtsps://192.0.2.1:7441/tok-high?enableSrtp",
        "medium": "rtsps://192.0.2.1:7441/tok-medium?enableSrtp",
        "low": "rtsps://192.0.2.1:7441/tok-low?enableSrtp",
        "package": None,
    })
    plug.api = api
    plug._last_rest_call = 0.0

    plug._refresh_stream_urls(dev)

    assert dev.states["streamUrlHigh"] == "rtsps://192.0.2.1:7441/tok-high?enableSrtp"
    assert dev.states["streamUrlMedium"] == "rtsps://192.0.2.1:7441/tok-medium?enableSrtp"
    assert dev.states["streamUrlLow"] == "rtsps://192.0.2.1:7441/tok-low?enableSrtp"
    assert dev.states["streamUrlPackage"] == "", "a null package must become '' not None"
    assert api.get_calls == ["cam-1"]
    assert api.create_calls == [], "GET already had non-null values -- POST must not fire"


def test_stream_url_token_never_appears_in_any_log_record_on_success(fake_indigo, caplog):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    token = "rtsps://192.0.2.1:7441/SECRET-TOKEN-abcdef?enableSrtp"
    api = _FakeStreamAPI({"high": token, "medium": token, "low": token, "package": None})
    plug.api = api
    plug._last_rest_call = 0.0

    with caplog.at_level("DEBUG"):
        plug._refresh_stream_urls(dev)

    for record in caplog.records:
        assert "SECRET-TOKEN" not in record.getMessage(), (
            "the stream URL/token must never appear in a log record"
        )
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("refreshed" in r.getMessage() for r in info_records), (
        "the token-absence check above must not be passing on a silent no-op"
    )


def test_stream_url_token_never_appears_in_any_log_record_on_error(fake_indigo, caplog):
    """The fake API's ProtectAPIError carries the token in .body, exactly
    like the real endpoint's error body could -- the log line must not
    surface it via str(exc) either."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    token = "rtsps://192.0.2.1:7441/SECRET-TOKEN-abcdef?enableSrtp"

    class LeakingErrorAPI:
        def get_rtsps_streams(self, camera_id):
            raise ProtectAPIError(
                f"HTTP 500 for /cameras/{camera_id}/rtsps-stream", status=500, body=token)

    plug.api = LeakingErrorAPI()
    plug._last_rest_call = 0.0

    with caplog.at_level("DEBUG"):
        plug._refresh_stream_urls(dev)

    for record in caplog.records:
        assert "SECRET-TOKEN" not in record.getMessage(), (
            "the stream URL/token must never appear in a log record, even via "
            "the error path"
        )
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("could not refresh stream URLs" in r.getMessage() for r in error_records), (
        "the token-absence check above must not be passing on a silent no-op"
    )


def test_stream_urls_get_all_null_triggers_post_without_package_by_default(fake_indigo):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    api = _FakeStreamAPI(
        {"high": None, "medium": None, "low": None, "package": None},
        create_response={
            "high": "rtsps://x/1", "medium": "rtsps://x/2",
            "low": "rtsps://x/3", "package": None,
        },
    )
    plug.api = api
    plug._last_rest_call = 0.0
    plug.camera_info = {"cam-1": {"hasPackageCamera": False}}

    plug._refresh_stream_urls(dev)

    assert api.create_calls == [("cam-1", ["high", "medium", "low"])]
    assert dev.states["streamUrlHigh"] == "rtsps://x/1"
    assert dev.states["streamUrlPackage"] == ""


def test_stream_urls_get_all_null_with_package_camera_includes_package(fake_indigo):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    api = _FakeStreamAPI(
        {"high": None, "medium": None, "low": None, "package": None},
        create_response={
            "high": "rtsps://x/1", "medium": "rtsps://x/2",
            "low": "rtsps://x/3", "package": "rtsps://x/4",
        },
    )
    plug.api = api
    plug._last_rest_call = 0.0
    plug.camera_info = {"cam-1": {"hasPackageCamera": True}}

    plug._refresh_stream_urls(dev)

    assert api.create_calls == [("cam-1", ["high", "medium", "low", "package"])]
    assert dev.states["streamUrlPackage"] == "rtsps://x/4"


def test_partial_null_get_posts_missing_when_camera_cached(fake_indigo):
    """Camera capabilities ARE cached: a null 'high' triggers a POST for
    just that quality, and a real response replaces the prior value."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    dev.states["streamUrlHigh"] = "rtsps://x/prior-high"
    api = _FakeStreamAPI(
        {"high": None, "medium": "rtsps://x/m", "low": "rtsps://x/l", "package": None},
        create_response={"high": "rtsps://x/new-high"},
    )
    plug.api = api
    plug._last_rest_call = 0.0
    plug.camera_info = {"cam-1": {"hasPackageCamera": False}}

    plug._refresh_stream_urls(dev)

    assert api.create_calls == [("cam-1", ["high"])]
    assert dev.states["streamUrlHigh"] == "rtsps://x/new-high"


def test_partial_null_get_keeps_prior_when_camera_not_cached_and_warns(fake_indigo, caplog):
    """Camera capabilities are NOT cached (e.g. before the first camera
    refresh): a null 'high' must not trigger a guessed POST, the prior
    value is kept, and both conditions are named in a WARNING."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    dev.states["streamUrlHigh"] = "rtsps://x/prior-high"
    api = _FakeStreamAPI(
        {"high": None, "medium": "rtsps://x/m", "low": "rtsps://x/l", "package": None})
    plug.api = api
    plug._last_rest_call = 0.0
    plug.camera_info = {}   # not cached

    with caplog.at_level("WARNING"):
        plug._refresh_stream_urls(dev)

    assert api.create_calls == []
    assert dev.states["streamUrlHigh"] == "rtsps://x/prior-high", "prior value must survive"
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("capabilities not loaded" in r.getMessage() for r in warning_records)
    assert any("kept previous URL for" in r.getMessage() and "high" in r.getMessage()
               for r in warning_records)


@pytest.mark.parametrize("bad_body", [{}, {"error": "x"}])
def test_shape_error_when_no_known_keys_present(fake_indigo, caplog, bad_body):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    dev.states["streamUrlHigh"] = "rtsps://x/prior-high"

    class ShapeErrorAPI:
        def get_rtsps_streams(self, camera_id):
            return dict(bad_body)

        def create_rtsps_streams(self, camera_id, qualities):
            raise AssertionError("must not POST on a shape error")

    plug.api = ShapeErrorAPI()
    plug._last_rest_call = 0.0

    with caplog.at_level("ERROR"):
        plug._refresh_stream_urls(dev)

    assert dev.states["streamUrlHigh"] == "rtsps://x/prior-high", "states must be untouched"
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("unexpected response shape" in r.getMessage() for r in error_records)


def test_non_string_value_for_a_key_is_skipped_with_warning(fake_indigo, caplog):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)

    class WeirdShapeAPI:
        def get_rtsps_streams(self, camera_id):
            return {"high": 12345, "medium": "rtsps://x/m", "low": "rtsps://x/l",
                     "package": None}

        def create_rtsps_streams(self, camera_id, qualities):
            raise AssertionError(
                "camera_info is deliberately empty here -- capabilities are "
                "unknown, so no guessed POST should ever be attempted"
            )

    plug.api = WeirdShapeAPI()
    plug._last_rest_call = 0.0
    # camera_info deliberately empty -- isolates this test to the
    # skip-and-warn behaviour, not the POST/cache branch covered above.

    with caplog.at_level("WARNING"):
        plug._refresh_stream_urls(dev)

    assert dev.states["streamUrlHigh"] == "", "a non-string value must not be written verbatim"
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("non-string value" in r.getMessage() and "high" in r.getMessage()
               for r in warning_records)


def test_stream_urls_error_leaves_prior_states_untouched_and_logs_stale(fake_indigo, caplog):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)
    dev.states["streamUrlHigh"] = "rtsps://x/still-good"

    class FailingAPI:
        def get_rtsps_streams(self, camera_id):
            raise ProtectAPIError("HTTP 500 for /cameras/cam-1/rtsps-stream", status=500)

    plug.api = FailingAPI()
    plug._last_rest_call = 0.0

    with caplog.at_level("ERROR"):
        plug._refresh_stream_urls(dev)

    assert dev.states["streamUrlHigh"] == "rtsps://x/still-good", (
        "a transient failure must not blank a working stored URL"
    )
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("stale" in r.getMessage() for r in error_records), (
        "the error log must say the stored URLs may now be stale"
    )


def test_stream_urls_error_with_nothing_stored_says_not_stale(fake_indigo, caplog):
    """The wording must not claim staleness for a URL that was never
    fetched in the first place."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True)

    class FailingAPI:
        def get_rtsps_streams(self, camera_id):
            raise ProtectAPIError("HTTP 500 for /cameras/cam-1/rtsps-stream", status=500)

    plug.api = FailingAPI()
    plug._last_rest_call = 0.0

    with caplog.at_level("ERROR"):
        plug._refresh_stream_urls(dev)

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("no URLs are stored yet" in r.getMessage() for r in error_records)
    assert not any("stale" in r.getMessage() for r in error_records)


def test_device_start_comm_default_opted_out_writes_empty_stream_states(fake_indigo):
    """End-to-end through the real lifecycle call, not just the unit-level
    _refresh_stream_urls call the other tests above use directly."""
    plug = make_plugin({})   # unconfigured -- self.api stays None
    dev = add_camera_device(fake_indigo, plug)

    assert dev.states["streamUrlHigh"] == ""
    assert dev.states["streamUrlMedium"] == ""
    assert dev.states["streamUrlLow"] == ""
    assert dev.states["streamUrlPackage"] == ""


def test_device_start_comm_opted_in_queues_pending_without_rest(fake_indigo):
    """deviceStartComm must be cheap-only (threading review): an opted-in
    device gets QUEUED, not fetched -- no stream state is written yet, and
    a fatal-collaborator API proves the network was never touched, whether
    or not self.api is even configured."""
    plug = make_plugin({})
    plug.api = _RaisingStreamAPI()
    dev = add_camera_device_with_props(
        fake_indigo, plug, {"exposeStreamUrls": True})   # must not raise, must not fetch

    for key in plugin_module.STREAM_URL_STATES.values():
        assert key not in dev.states, f"{key} must not be written by deviceStartComm itself"
    assert dev.id in plug._stream_refresh_pending


def test_assert_no_url_in_message_trips_on_leak():
    """Pins _assert_no_url_in_message's actual enforcement, the same way
    protect_api's own test_assert_no_secret_trips_on_leak pins its guard --
    if this check is ever weakened, THIS test fails immediately."""
    token = "rtsps://192.0.2.1:7441/should-not-leak"
    with pytest.raises(AssertionError):
        plugin_module._assert_no_url_in_message(
            f"oops the url is {token} right here", {"high": token})


# ---------------------------------------------------------------------
# Issue #7 follow-up (threading/socket-safety review): the synchronous
# user-initiated paths must always report something, and the async
# pump-drain path must never be able to take the event socket down.
# ---------------------------------------------------------------------

def test_refresh_stream_urls_action_opted_out_logs_info_and_clears(fake_indigo, caplog):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=False)
    dev.states["streamUrlHigh"] = "rtsps://x/prior"
    plug.api = _RaisingStreamAPI()

    with caplog.at_level("INFO"):
        plug.refreshStreamUrls(object(), dev)

    assert dev.states["streamUrlHigh"] == ""
    assert dev.states["streamUrlMedium"] == ""
    assert dev.states["streamUrlLow"] == ""
    assert dev.states["streamUrlPackage"] == ""
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("tick 'Expose RTSPS stream URLs'" in r.getMessage() for r in info_records)


def test_refresh_stream_urls_action_unconfigured_logs_error(fake_indigo, caplog):
    plug = make_plugin({})   # self.api stays None
    dev = _make_stream_device(fake_indigo, expose_value=True)

    with caplog.at_level("ERROR"):
        plug.refreshStreamUrls(object(), dev)

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("not configured" in r.getMessage() for r in error_records)


class _RequestStatusAction:
    deviceAction = indigo.kUniversalAction.RequestStatus


def test_request_status_opted_out_logs_info_and_clears(fake_indigo, caplog):
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=False)
    dev.states["streamUrlHigh"] = "rtsps://x/prior"
    plug.api = _RaisingStreamAPI()

    with caplog.at_level("INFO"):
        plug.actionControlUniversal(_RequestStatusAction(), dev)

    assert dev.states["streamUrlHigh"] == ""
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("tick 'Expose RTSPS stream URLs'" in r.getMessage() for r in info_records)


def test_request_status_unconfigured_logs_error(fake_indigo, caplog):
    plug = make_plugin({})   # self.api stays None
    dev = _make_stream_device(fake_indigo, expose_value=True)

    with caplog.at_level("ERROR"):
        plug.actionControlUniversal(_RequestStatusAction(), dev)

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("not configured" in r.getMessage() for r in error_records)


def test_pump_drains_one_pending_stream_refresh_per_tick(fake_indigo):
    plug = make_plugin({})
    dev_a = _make_stream_device(fake_indigo, expose_value=True, dev_id=1, camera_id="cam-a")
    dev_b = _make_stream_device(fake_indigo, expose_value=True, dev_id=2, camera_id="cam-b")
    _prep_pending(plug, dev_a, camera_id="cam-a")
    _prep_pending(plug, dev_b, camera_id="cam-b")
    plug.api = _FakeStreamAPI({"high": "rtsps://x/h", "medium": "rtsps://x/m",
                                "low": "rtsps://x/l", "package": None})
    plug._last_rest_call = 0.0

    class TwoReadSocket:
        last_frame_at = time.monotonic()

        def __init__(self):
            self.reads = 0

        def read_message(self, timeout=1.0):
            self.reads += 1
            if self.reads > 2:
                raise plug.StopThread()
            return None

        def send_ping(self):
            pass

    plug.socket = TwoReadSocket()

    with pytest.raises(plug.StopThread):
        plug._pump()   # must NOT raise ConnectionError -- socket stays "connected"

    assert plug._stream_refresh_pending == set(), "both devices must drain, one per tick"
    assert dev_a.states["streamUrlHigh"] == "rtsps://x/h"
    assert dev_b.states["streamUrlHigh"] == "rtsps://x/h"


def test_pump_drain_survives_a_device_write_failure_and_logs_untick(fake_indigo, caplog):
    """Fatal-collaborator form, at the write step: updateStatesOnServer
    raises. That must be caught by the drain, not by runConcurrentThread's
    generic handler, which would tear the socket down over one bad camera.
    """
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True, dev_id=1, camera_id="cam-1")
    _prep_pending(plug, dev)
    plug.api = _FakeStreamAPI({"high": "rtsps://x/h", "medium": "rtsps://x/m",
                                "low": "rtsps://x/l", "package": None})
    plug._last_rest_call = 0.0

    def boom(states):
        raise RuntimeError("Indigo server busy")

    dev.updateStatesOnServer = boom

    class OneReadSocket:
        last_frame_at = time.monotonic()

        def __init__(self):
            self.reads = 0

        def read_message(self, timeout=1.0):
            self.reads += 1
            if self.reads > 1:
                raise plug.StopThread()
            return None

        def send_ping(self):
            pass

    plug.socket = OneReadSocket()

    with caplog.at_level("ERROR"):
        with pytest.raises(plug.StopThread):
            plug._pump()   # must NOT raise the RuntimeError

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("untick" in r.getMessage() for r in error_records)


def test_leak_guard_failure_is_caught_by_the_pump_drain_not_raised(
        fake_indigo, caplog, monkeypatch):
    """Guard-wiring test: force _assert_no_url_in_message itself to raise,
    and prove the drain still turns that into the 'untick' ERROR rather
    than letting it escape."""
    plug = make_plugin({})
    dev = _make_stream_device(fake_indigo, expose_value=True, dev_id=1, camera_id="cam-1")
    _prep_pending(plug, dev)
    plug.api = _FakeStreamAPI({"high": "rtsps://x/h", "medium": "rtsps://x/m",
                                "low": "rtsps://x/l", "package": None})
    plug._last_rest_call = 0.0

    def exploding_guard(message, *sources):
        raise AssertionError("leak guard tripped")

    monkeypatch.setattr(plugin_module, "_assert_no_url_in_message", exploding_guard)

    with caplog.at_level("ERROR"):
        plug._drain_one_pending_stream_refresh()   # must not raise

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("untick" in r.getMessage() for r in error_records)


def test_open_socket_primes_but_never_fetches_stream_urls(fake_indigo, monkeypatch):
    """_open_socket must only PRIME (queue) opted-in devices, never fetch --
    a get_rtsps_streams/create_rtsps_streams that raises must not be able
    to break the reconnect path, and must never even be called from here."""
    plug = make_plugin({"host": "h", "apiKey": "k"})
    dev = _make_stream_device(fake_indigo, expose_value=True, dev_id=1, camera_id="cam-1")
    plug.cameras = {"cam-1": {1}}
    plug.api = _RaisingStreamAPI()
    plug._last_rest_call = 0.0

    class FakeSocket:
        def connect(self):
            pass

    monkeypatch.setattr(plugin_module, "ProtectEventSocket", lambda *a, **k: FakeSocket())

    plug._open_socket()   # must not raise

    assert dev.id in plug._stream_refresh_pending, "opted-in device must be queued, not fetched"
# Issue #8: sensors/lights/chimes/NVR.
#
# Spec-derived (OpenAPI v6.2.83) -- UNVERIFIED against real hardware, the
# reference rig's /sensors, /lights, /chimes all return []. The question,
# per workspace convention: when could this report idle/unavailable/kept
# and be wrong, or touch an API it was never told exists?
# ---------------------------------------------------------------------

def add_sensor_device(fake_indigo, plug, sensor_id="sensor-1", dev_id=3001, name="Front Door",
                       extra_props=None):
    from conftest import _FakeDevice
    props = {"sensorId": sensor_id}
    props.update(extra_props or {})
    dev = _FakeDevice(dev_id, name=name, device_type_id="protectSensor", plugin_props=props)
    fake_indigo.devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


def add_light_device(fake_indigo, plug, light_id="light-1", dev_id=3002, name="Floodlight"):
    from conftest import _FakeDevice
    dev = _FakeDevice(dev_id, name=name, device_type_id="protectLight",
                       plugin_props={"lightId": light_id})
    fake_indigo.devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


def add_chime_device(fake_indigo, plug, chime_id="chime-1", dev_id=3003, name="Chime"):
    from conftest import _FakeDevice
    dev = _FakeDevice(dev_id, name=name, device_type_id="protectChime",
                       plugin_props={"chimeId": chime_id})
    fake_indigo.devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


def add_nvr_device(fake_indigo, plug, dev_id=3004, name="UNVR"):
    from conftest import _FakeDevice
    dev = _FakeDevice(dev_id, name=name, device_type_id="protectNvr", plugin_props={})
    fake_indigo.devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


class _FatalNonCameraAPI:
    """Fatal-collaborator: every method raises if called. Proves
    _poll_devices only touches the classes that actually have a registered
    device -- not merely that it 'usually' skips the others."""

    def get_sensors(self):
        raise AssertionError("get_sensors must not be called with no sensor registered")

    def get_lights(self):
        raise AssertionError("get_lights must not be called with no light registered")

    def get_chimes(self):
        raise AssertionError("get_chimes must not be called with no chime registered")

    def get_nvr(self):
        raise AssertionError("get_nvr must not be called with no NVR registered")


def test_poll_devices_touches_nothing_with_only_cameras_registered(fake_indigo):
    plug = make_plugin({})
    add_camera_device(fake_indigo, plug)
    plug.api = _FatalNonCameraAPI()
    plug._last_rest_call = 0.0

    plug._poll_devices()   # must not raise


def test_poll_devices_with_one_sensor_only_fetches_sensors(fake_indigo):
    plug = make_plugin({})
    add_sensor_device(fake_indigo, plug)

    class SensorOnlyAPI(_FatalNonCameraAPI):
        def get_sensors(self):
            return []

    plug.api = SensorOnlyAPI()
    plug._last_rest_call = 0.0

    plug._poll_devices()   # must not raise touching lights/chimes/nvr


def test_sensor_poll_failure_keeps_last_values_and_logs_once_per_outage(fake_indigo, caplog):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)

    class WorkingAPI:
        def get_sensors(self):
            return [{"id": "sensor-1", "state": "CONNECTED", "mountType": "door",
                      "isOpened": False}]

    plug.api = WorkingAPI()
    plug._last_rest_call = 0.0
    plug._poll_sensors()
    assert dev.states["sensorState"] == "CONNECTED"
    writes_after_success = len(dev.state_writes)

    class FailingAPI:
        def get_sensors(self):
            raise ProtectAPIError("HTTP 500 for /sensors", status=500)

    plug.api = FailingAPI()
    plug._last_rest_call = 0.0

    with caplog.at_level("ERROR"):
        plug._poll_sensors()
        plug._poll_sensors()   # a second consecutive failure

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1, "ERROR must log once per class per outage, not per poll"
    assert dev.states["sensorState"] == "CONNECTED", "last-known value must survive a poll failure"
    assert len(dev.state_writes) == writes_after_success, (
        "a failed poll must not write any new state batch -- lastPoll included"
    )


@pytest.mark.parametrize("mount_type,expected", [
    ("door", "open"), ("window", "open"), ("garage", "open"),
    ("leak", "leak"), ("none", "motion"), ("unknown-mount-type", "motion"),
])
def test_primary_state_auto_maps_mount_type(fake_indigo, mount_type, expected):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)   # primaryState defaults to "auto"
    assert plug._resolve_sensor_primary_state(dev, mount_type) == expected


def test_primary_state_explicit_choice_overrides_mount_type(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug, extra_props={"primaryState": "alarm"})
    assert plug._resolve_sensor_primary_state(dev, "door") == "alarm"


def test_primary_state_auto_open_drives_on_off_state_for_door_mount(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.sensor_info = {"sensor-1": {"state": "CONNECTED", "mountType": "door",
                                      "isOpened": True, "openStatusChangedAt": 1}}

    plug._apply_sensor_state("sensor-1", force=True)

    assert dev.states["onOffState"] is True
    assert dev.states["isOpen"] is True
    assert dev.states["motionDetected"] is False


def test_is_open_pulse_newer_than_poll_wins(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.sensor_info = {"sensor-1": {"isOpened": False, "openStatusChangedAt": 100}}
    plug.tracker.handle({"item": {"id": "op1", "device": "sensor-1", "type": "sensorOpened",
                                   "start": 200,
                                   "metadata": {"sensorMountType": {"text": "door"}}}})

    plug._apply_sensor_state("sensor-1", force=True)

    assert dev.states["isOpen"] is True, "a pulse newer than the poll's openStatusChangedAt must win"


def test_is_open_poll_newer_than_pulse_wins(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.tracker.handle({"item": {"id": "op1", "device": "sensor-1", "type": "sensorOpened",
                                   "start": 100,
                                   "metadata": {"sensorMountType": {"text": "door"}}}})
    plug.sensor_info = {"sensor-1": {"isOpened": False, "openStatusChangedAt": 200}}

    plug._apply_sensor_state("sensor-1", force=True)

    assert dev.states["isOpen"] is False, "a poll newer than the pulse must win"


def test_poll_motion_false_clears_stuck_tracker_family(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.tracker.handle({"item": {"id": "sm1", "device": "sensor-1",
                                   "type": "sensorMotion", "start": 1}})
    assert plug.tracker.family_active(plugin_module.FAMILY_SENSOR_MOTION, "sensor-1") is True

    class WorkingAPI:
        def get_sensors(self):
            return [{"id": "sensor-1", "state": "CONNECTED", "isMotionDetected": False}]

    plug.api = WorkingAPI()
    plug._last_rest_call = 0.0

    plug._poll_sensors()

    assert plug.tracker.family_active(plugin_module.FAMILY_SENSOR_MOTION, "sensor-1") is False
    assert dev.states["motionDetected"] is False


def test_battery_level_is_a_native_write_not_a_batched_state(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.sensor_info = {"sensor-1": {"batteryStatus": {"percentage": 73, "isLow": False}}}

    plug._apply_sensor_state("sensor-1", force=True)

    assert dev.states["batteryLevel"] == 73
    assert isinstance(dev.states["batteryLevel"], int)
    # updateStateOnServer batches are recorded as single-entry lists in the
    # fake; batteryLevel must never appear inside the multi-key
    # updateStatesOnServer batch (it is not a declared <State>).
    for batch in dev.state_writes:
        if len(batch) > 1:
            assert "batteryLevel" not in {entry["key"] for entry in batch}


def test_disconnect_forces_sensor_lifecycle_booleans_false_and_keeps_temperature(fake_indigo):
    plug = make_plugin({})
    dev = add_sensor_device(fake_indigo, plug)
    plug.socket = object()
    plug.tracker.handle({"item": {"id": "sm1", "device": "sensor-1",
                                   "type": "sensorMotion", "start": 1}})
    plug.tracker.handle({"item": {"id": "sl1", "device": "sensor-1", "type": "sensorWaterLeak",
                                   "start": 2, "metadata": {"sensorMountType": {"text": "leak"}}}})
    plug.sensor_info = {"sensor-1": {"stats": {"temperature": {"value": 21.0}}}}
    plug._apply_sensor_state("sensor-1", force=True)
    assert dev.states["motionDetected"] is True
    assert dev.states["leakDetected"] is True
    assert dev.states["temperature"] == 21.0

    plug._mark_all_disconnected()

    assert dev.states["motionDetected"] is False
    assert dev.states["leakDetected"] is False
    assert dev.states["connected"] is False
    assert dev.states["temperature"] == 21.0, "poll-derived values must survive a disconnect"


def test_disconnect_forces_light_pir_motion_false_and_keeps_is_light_on(fake_indigo):
    plug = make_plugin({})
    dev = add_light_device(fake_indigo, plug)
    plug.socket = object()
    plug.light_info = {"light-1": {"isLightOn": True, "isPirMotionDetected": True}}
    plug.tracker.handle({"item": {"id": "lm1", "device": "light-1",
                                   "type": "lightMotion", "start": 1}})
    plug._apply_light_state("light-1", force=True)
    assert dev.states["pirMotionDetected"] is True
    assert dev.states["onOffState"] is True

    plug._mark_all_disconnected()

    assert dev.states["pirMotionDetected"] is False
    assert dev.states["onOffState"] is True, "poll-derived onOffState (isLightOn) must survive a disconnect"


def test_light_turn_on_patches_force_enabled_then_gets_then_writes_states(fake_indigo):
    plug = make_plugin({})
    dev = add_light_device(fake_indigo, plug)

    class RecordingAPI:
        def __init__(self):
            self.calls = []

        def patch_light(self, light_id, body):
            self.calls.append(("patch", light_id, dict(body)))
            return {}

        def get_light(self, light_id):
            self.calls.append(("get", light_id))
            return {"id": light_id, "state": "CONNECTED", "isLightOn": True,
                     "isLightForceEnabled": True}

    api = RecordingAPI()
    plug.api = api
    plug._last_rest_call = 0.0

    action = type("Action", (), {"deviceAction": indigo.kDeviceAction.TurnOn})()
    plug.actionControlDevice(action, dev)

    assert api.calls == [
        ("patch", "light-1", {"isLightForceEnabled": True}),
        ("get", "light-1"),
    ], "PATCH must happen before the re-GET"
    assert dev.states["forceEnabled"] is True
    assert dev.states["onOffState"] is True


def test_light_patch_refused_logs_error_and_leaves_states_unchanged(fake_indigo, caplog):
    plug = make_plugin({})
    dev = add_light_device(fake_indigo, plug)
    dev.states["onOffState"] = False
    writes_before = len(dev.state_writes)

    class FailingAPI:
        def patch_light(self, light_id, body):
            raise ProtectAPIError("HTTP 400 for /lights/light-1", status=400)

        def get_light(self, light_id):
            raise AssertionError("get_light must not be called when the patch failed")

    plug.api = FailingAPI()
    plug._last_rest_call = 0.0

    action = type("Action", (), {"deviceAction": indigo.kDeviceAction.TurnOn})()
    with caplog.at_level("ERROR"):
        plug.actionControlDevice(action, dev)

    assert dev.states["onOffState"] is False
    assert len(dev.state_writes) == writes_before
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_set_light_level_out_of_range_errors_without_patching(fake_indigo, caplog):
    plug = make_plugin({})
    dev = add_light_device(fake_indigo, plug)

    class RaisingAPI:
        def patch_light(self, light_id, body):
            raise AssertionError("patch_light must not be called for an out-of-range level")

    plug.api = RaisingAPI()
    plug._last_rest_call = 0.0

    action = type("Action", (), {"props": {"ledLevel": "7"}})()
    with caplog.at_level("ERROR"):
        plug.setLightLevel(action, dev)

    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_set_chime_volume_with_no_ring_settings_errors_without_patching(fake_indigo, caplog):
    plug = make_plugin({})
    dev = add_chime_device(fake_indigo, plug)
    plug.chime_info = {"chime-1": {"id": "chime-1", "state": "CONNECTED", "ringSettings": []}}

    class RaisingAPI:
        def patch_chime(self, chime_id, body):
            raise AssertionError("patch_chime must not be called with no ringSettings")

    plug.api = RaisingAPI()
    plug._last_rest_call = 0.0

    action = type("Action", (), {"props": {"volume": "50"}})()
    with caplog.at_level("ERROR"):
        plug.setChimeVolume(action, dev)

    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_set_chime_volume_replaces_every_entrys_volume(fake_indigo):
    plug = make_plugin({})
    dev = add_chime_device(fake_indigo, plug)
    plug.chime_info = {"chime-1": {"id": "chime-1", "state": "CONNECTED", "ringSettings": [
        {"cameraId": "cam-a", "repeatTimes": 1, "ringtoneId": "r1", "volume": 10},
        {"cameraId": "cam-b", "repeatTimes": 2, "ringtoneId": "r2", "volume": 20},
    ]}}

    class RecordingAPI:
        def __init__(self):
            self.patch_calls = []

        def patch_chime(self, chime_id, body):
            self.patch_calls.append((chime_id, body))
            return {"id": chime_id, "state": "CONNECTED", "ringSettings": body["ringSettings"]}

    api = RecordingAPI()
    plug.api = api
    plug._last_rest_call = 0.0

    action = type("Action", (), {"props": {"volume": "75"}})()
    plug.setChimeVolume(action, dev)

    assert api.patch_calls[0][1]["ringSettings"] == [
        {"cameraId": "cam-a", "repeatTimes": 1, "ringtoneId": "r1", "volume": 75},
        {"cameraId": "cam-b", "repeatTimes": 2, "ringtoneId": "r2", "volume": 75},
    ]
    assert dev.states["ringVolume"] == 75


def test_nvr_arm_mode_absent_reports_unavailable(fake_indigo):
    plug = make_plugin({})
    dev = add_nvr_device(fake_indigo, plug)
    plug.nvr_info = {"id": "nvr1", "modelKey": "nvr", "name": "UNVR"}   # no armMode key

    plug._apply_nvr_state(force=True)

    assert dev.states["armStatus"] == "unavailable"


def test_nvr_never_polled_reports_unavailable(fake_indigo):
    plug = make_plugin({})
    dev = add_nvr_device(fake_indigo, plug)   # nvr_info stays None

    assert dev.states["armStatus"] == "unavailable"


def test_nvr_rekeys_from_placeholder_to_real_id_on_first_poll(fake_indigo):
    plug = make_plugin({})
    dev = add_nvr_device(fake_indigo, plug)
    assert "nvr" in plug.nvrs

    class WorkingAPI:
        def get_nvr(self):
            return {"id": "real-nvr-id", "modelKey": "nvr", "name": "UNVR",
                     "armMode": {"status": "disabled"}}
        def get_meta_info(self):
            return {"applicationVersion": "7.2.105"}

    plug.api = WorkingAPI()
    plug._last_rest_call = 0.0

    plug._poll_nvr()

    assert "nvr" not in plug.nvrs
    assert plug.nvrs["real-nvr-id"] == {dev.id}
    assert dev.states["armStatus"] == "disabled"
    assert dev.states["protectVersion"] == "7.2.105"
