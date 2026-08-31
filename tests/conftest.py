"""Shared pytest fixtures.

Why this file is not a MagicMock
--------------------------------
The obvious stub is ``sys.modules["indigo"] = MagicMock()``. It is a trap.
``class Plugin(indigo.PluginBase)`` then resolves through
``MagicMock.__mro_entries__``, so ``plugin.Plugin`` is not a class at all --
it is a MagicMock. Every attribute access on it, and on any "instance", returns
another MagicMock. A test written against that passes without executing a
single line of plugin code, *including* a degradation-path test whose whole
design is to make a negative assertion fatal.

That was the state of this file, and it was caught by review rather than by a
failing test -- which is exactly the point. So: real classes, real exceptions,
real method bodies. If a stub gets so elaborate that it needs its own tests,
that is the signal to move the logic out of ``plugin.py`` instead.
"""

import logging
import sys
import types
from pathlib import Path

import pytest

SERVER_PLUGIN_DIR = (
    Path(__file__).parent.parent
    / "UniFi Protect.indigoPlugin"
    / "Contents"
    / "Server Plugin"
)
sys.path.insert(0, str(SERVER_PLUGIN_DIR))


class _OrderingViolation(BaseException):
    """Raised (never asserted) by ``_FakeDevice.stateListOrDisplayStateIdChanged``
    when it is called after a state write has already happened. It derives
    from ``BaseException``, not ``Exception``/``AssertionError``, because
    plugin.py wraps that call in ``except Exception`` -- an AssertionError
    would be silently swallowed there, hiding the very ordering bug this
    exists to catch.
    """


class _FakeDevice:
    """Just enough of an Indigo device to record what a plugin writes to it."""

    def __init__(self, dev_id, name="Camera", plugin_props=None, enabled=True):
        self.id = dev_id
        self.name = name
        self.pluginProps = dict(plugin_props or {})
        self.enabled = enabled
        self.states = {}
        self.deviceTypeId = "protectCamera"
        self.model = "Protect Camera"
        # Full history, so a test can assert on the *sequence* of writes and
        # not merely the final resting state.
        self.state_writes = []
        self.image_writes = []
        self.replace_on_server_calls = 0
        self.state_list_changed_calls = 0
        # Set to an exception instance to make stateListOrDisplayStateIdChanged
        # raise, for testing the deviceStartComm degradation path.
        self.state_list_changed_raises = None

    def updateStateOnServer(self, key, value=None, **kwargs):
        self.states[key] = value
        self.state_writes.append([{"key": key, "value": value}])

    def updateStatesOnServer(self, states):
        for entry in states:
            self.states[entry["key"]] = entry["value"]
        self.state_writes.append(list(states))

    def updateStateImageOnServer(self, image):
        self.image_writes.append(image)

    def replaceOnServer(self):
        self.replace_on_server_calls += 1
    def stateListOrDisplayStateIdChanged(self):
        # Fatal ordering check, first call only: if THE FIRST call for this
        # device happens after a state write, a plugin upgrade adding new
        # Devices.xml states would silently drop writes to them -- this must
        # run before the first write. Guarded to the first call (not "no
        # writes ever") because the invariant is per-start, not per-lifetime:
        # a deviceStartComm -> deviceStopComm -> deviceStartComm sequence
        # legitimately has writes on the device before the second call.
        # Raises _OrderingViolation rather than asserting, because plugin.py
        # wraps this call in `except Exception`, which would otherwise hide
        # the failure instead of surfacing it as a test failure.
        if self.state_list_changed_calls == 0 and self.state_writes:
            raise _OrderingViolation(
                f"{self.name}: stateListOrDisplayStateIdChanged() called after "
                "state writes had already started - it must run first"
            )
        self.state_list_changed_calls += 1
        if self.state_list_changed_raises is not None:
            raise self.state_list_changed_raises


class _FakeDevices:
    """Mimics ``indigo.devices``, including its get(id, default) form."""

    def __init__(self):
        self._devices = {}

    def add(self, dev):
        self._devices[dev.id] = dev
        return dev

    def get(self, dev_id, default=None):
        return self._devices.get(dev_id, default)

    def __getitem__(self, dev_id):
        return self._devices[dev_id]

    def __contains__(self, dev_id):
        return dev_id in self._devices

    def iter(self, filter=None):  # noqa: A002 - matches Indigo's signature
        return list(self._devices.values())


class _FakePluginBase:
    """Stand-in for ``indigo.PluginBase``.

    ``StopThread`` subclasses ``Exception`` exactly as Indigo's own
    ``plugin_base.py`` does -- that detail is load-bearing, because it is why a
    bare ``except Exception`` in ``runConcurrentThread`` swallows the shutdown
    signal.
    """

    class StopThread(Exception):
        pass

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.stopThread = False
        self.sleep_calls = []
        # Raise StopThread once this many sleeps have happened. None = never,
        # which lets a test drive a bounded number of reconnect cycles and then
        # unwind deterministically.
        self.stop_after_sleeps = None

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        if self.stopThread:
            raise self.StopThread()
        if (self.stop_after_sleeps is not None
                and len(self.sleep_calls) >= self.stop_after_sleeps):
            raise self.StopThread()

    def debugLog(self, msg):
        self.logger.debug(msg)


class _StateImageSel:
    MotionSensor = "MotionSensor"
    MotionSensorTripped = "MotionSensorTripped"
    SensorOff = "SensorOff"
    SensorOn = "SensorOn"


class _UniversalAction:
    RequestStatus = "RequestStatus"
    Beep = "Beep"
    EnergyUpdate = "EnergyUpdate"


def _install_fake_indigo():
    fake = types.ModuleType("indigo")
    fake.PluginBase = _FakePluginBase
    fake.Device = _FakeDevice
    fake.devices = _FakeDevices()
    fake.Dict = dict
    fake.List = list
    fake.kStateImageSel = _StateImageSel
    sys.modules["indigo"] = fake
    return fake


_install_fake_indigo()


@pytest.fixture
def fake_indigo():
    """A clean ``indigo.devices`` per test, so device state never leaks between them.

    Note it resets the EXISTING module rather than installing a new one.
    ``plugin.py`` does ``import indigo`` at module scope and binds that object
    once; swapping ``sys.modules["indigo"]`` afterwards leaves the plugin
    holding the old module, so a test would add devices to a registry the code
    under test never reads -- and the test fails for a reason that has nothing
    to do with the plugin.
    """
    fake = sys.modules["indigo"]
    fake.devices = _FakeDevices()
    yield fake


@pytest.fixture
def make_device():
    def _make(dev_id=1001, camera_id="cam-1", name="Patio", enabled=True):
        return _FakeDevice(dev_id, name=name,
                           plugin_props={"cameraId": camera_id}, enabled=enabled)
    return _make
