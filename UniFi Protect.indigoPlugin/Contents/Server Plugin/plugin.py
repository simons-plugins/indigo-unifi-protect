#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UniFi Protect plugin for Indigo.

Motion state is driven entirely by the Protect event WebSocket. There is no
polling fallback and this is not an oversight: the integration API's camera
object carries no motion field and `GET /events` returns 404.

What that costs, stated plainly because it shapes every automation written
against this plugin: Indigo booleans cannot express "unknown". When the socket
is down this plugin forces `motionDetected` False and `connected` False --
and, identically, `audioDetected` plus the four specific audio states
(speechDetected/babyCryDetected/smokeAlarmDetected/coAlarmDetected). It
does NOT have a way to say "I cannot tell" in the motion state itself.

    Any trigger that acts on motion MUST gate on `connected` first.

Everything below exists to make `connected` trustworthy, because it is the only
signal that separates "nobody is there" from "I have no idea".
"""

import os
import time
from datetime import datetime

import indigo

from device_router import DeviceUpdateRouter, MISSING_MODEL_KEY, merge_update
from event_tracker import (
    EventTracker,
    FAMILY_SENSOR_ALARM,
    FAMILY_SENSOR_LEAK,
    FAMILY_SENSOR_MOTION,
    FAMILY_SENSOR_TAMPER,
    KNOWN_UNSUPPORTED_EVENT_TYPES,
    MISSING_TYPE_KEY,
)
from protect_api import ProtectAPI, ProtectAPIError
from protect_ws import ProtectEventSocket

# issue #18: modelKeys the /subscribe/devices spec documents but this plugin
# has no Indigo device type for. An unhandled key OUTSIDE this set is either
# a brand-new modelKey or a parsing gap -- worth a WARNING. One of these is
# expected/unremarkable -- DEBUG, mirroring KNOWN_UNSUPPORTED_EVENT_TYPES's
# same ring/DEBUG treatment on the events socket.
KNOWN_UNHANDLED_MODEL_KEYS = frozenset({
    "viewer", "speaker", "bridge", "aiprocessor", "aiport", "linkstation",
})

DEVICE_SOCKET_PATH = "/proxy/protect/integration/v1/subscribe/devices"

# The controller rate-limits. Measured on Protect 7.2.105: ~5 req/s earned
# HTTP 429; one request per 3s was clean. Nothing was measured in between, so
# 3.0 is the slowest known-good rate rather than a guess at the real ceiling.
# If you lower this, you are entering untested territory -- raise it back at
# the first 429.
MIN_REST_INTERVAL = 3.0

BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

# A connection must survive this long before its backoff is forgiven. Without
# it, a server that accepts the upgrade then immediately drops (Protect
# restarting, subscription refused) produces a hot 1s reconnect loop forever --
# each iteration also firing a GET /cameras at a controller that rate-limits.
STABLE_AFTER = 60.0

# The server sends NOTHING on an idle socket. Verified: a 5-minute live capture
# saw 10 frames during activity then 2 minutes of total silence on a healthy
# connection. So silence is not evidence of death and a plain "no frames for N
# seconds" watchdog would tear down good sockets every quiet night. We probe
# instead: send a ping, and require SOME frame (the pong counts) to come back.
PING_INTERVAL = 30.0
STALE_TIMEOUT = 90.0

TRACKED_DETECT_TYPES = ("person", "vehicle", "animal")

# Audio smartDetectTypes this plugin surfaces as their own boolean states,
# mapped to the state key each one writes.
TRACKED_AUDIO_TYPES = {
    "alrmSpeak": "speechDetected",
    "alrmBabyCry": "babyCryDetected",
    "alrmSmoke": "smokeAlarmDetected",
    "alrmCmonx": "coAlarmDetected",
}

# Which audio types count as "activity" for onOffState purposes, subject to
# the per-device audioCountsAsActivity checkbox. Smoke/CO alarms are
# deliberately NOT in here -- an alarm sound is not presence, and folding it
# into onOffState would make "device turned on" trigger on a smoke alarm,
# which is not what that trigger means and could bury a real alert under a
# routine motion notification.
PRESENCE_AUDIO_TYPES = frozenset({"alrmSpeak", "alrmBabyCry"})

# Written to cameraState when the camera list could not be fetched. An empty
# string is indistinguishable from "the camera genuinely reports no state", and
# a trigger reading it would see "not DISCONNECTED" and believe things are fine.
STATE_UNAVAILABLE = "unavailable"

# Protect's published OpenAPI enum for videoMode (issue #6). Only "default",
# "sport", and "slowShutter" have been observed on the reference rig; the
# rest come from the spec, unverified on the wire. Used only as the
# getVideoModeList dynamic-list fallback when a camera's own
# featureFlags.videoModes isn't cached yet -- the PATCH itself is always
# validated against the camera's own list, never this one.
VIDEO_MODE_SPEC_ENUM = (
    "default", "highFps", "sport", "slowShutter", "lprReflex", "lprNoneReflex",
)

# Legal ConfigUI values for the other four control actions (issue #6 review).
# Checked in the callback, not just trusted from the dialog -- a scripter can
# call executeAction() directly and supply anything, e.g. mode="ON" (which
# `mode == "on"` would silently read as False/off and log success).
LED_MODES = ("on", "off", "toggle")
OSD_TOGGLE_VALUES = ("unchanged", "on", "off")
OVERLAY_LOCATIONS = (
    "unchanged", "topLeft", "topMiddle", "topRight",
    "bottomLeft", "bottomMiddle", "bottomRight",
)
HDR_TYPES = ("auto", "on", "off")

# Legal ConfigUI values for the PTZ goto/patrol-start actions (issues
# #19/#21) -- the API's slot index, as a string (menu values arrive as
# strings). Protect's own UI numbers the same five slots 1-5.
PTZ_SLOTS = ("0", "1", "2", "3", "4")

# Legal ConfigUI checkbox fields for deleteStreamUrls (issue #25), same
# four qualities STREAM_URL_STATES already knows about.
RTSPS_DELETE_QUALITIES = ("high", "medium", "low", "package")

# Under Indigo's "Web Assets/images", so snapshots survive plugin upgrades and
# are servable to control pages at /images/<SNAPSHOT_SUBDIR>/...
SNAPSHOT_SUBDIR = "unifi-protect"

# Issue #27: the bundled HTML page this plugin auto-installs into Web Assets.
# The bundle folder name below is the actual on-disk ".indigoPlugin" name,
# not the CFBundleIdentifier -- it must match the repo's top-level directory.
WEB_PAGE_FILENAME = "cameras.html"
WEB_PAGE_BUNDLE_DIR = "UniFi Protect.indigoPlugin"

# Stream-quality key (as returned by the rtsps-stream endpoint) -> the state
# it is written to (issue #7). Order matters only for the log message below.
STREAM_URL_STATES = {
    "high": "streamUrlHigh",
    "medium": "streamUrlMedium",
    "low": "streamUrlLow",
    "package": "streamUrlPackage",
}

# Issue #8: sensors/lights/chimes/NVR poll cadence. REST-only -- chimes and
# the NVR have no event-socket feed at all; sensors/lights get live pulses
# and lifecycle events over the SAME camera event socket, but their
# measurements and config still need a periodic poll to catch up and to
# reconcile against. Spec-derived: none of this has ever run against real
# hardware (the reference rig's /sensors, /lights, /chimes all return []).
DEVICE_POLL_INTERVAL = 60.0

# protectSensor's `primaryState=auto` mapping: mount type -> which boolean
# state drives onOffState. Spec-derived (sensorMountType enum).
SENSOR_MOUNT_PRIMARY_STATE = {
    "door": "open", "window": "open", "garage": "open", "leak": "leak", "none": "motion",
}

# deviceTypeId -> (the ConfigUI field holding the selected Protect id, the
# noun used in the "no X selected" error). protectNvr is deliberately
# absent -- it has no picker, there is only ever one NVR.
_DEVICE_ID_FIELD_AND_LABEL = {
    "protectCamera": ("cameraId", "camera"),
    "protectSensor": ("sensorId", "sensor"),
    "protectLight": ("lightId", "light"),
    "protectChime": ("chimeId", "chime"),
}


def _truthy(value, default=True):
    """Coerce a pluginProps checkbox value to bool.

    Indigo can hand a checkbox prop back as the STRING "false" rather than
    the bool False (see heatmiser's `_coerce_bool`, indigo-matter's
    `export_dialog_mixin._truthy`), and `bool("false")` is True -- so a
    naive `.get(key, True)` would silently ignore a user unchecking the
    box. None (the prop was never set) resolves to `default`.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _camera_info_states(info):
    """Build the read-only hardware/config states from a cached camera
    object (issue #4). Split out of _write_states because pylint already
    flags that method for too-many-locals.

    Callers must only pass a non-None `info` -- when the camera object is
    unavailable there is nothing honest to report here, and the caller must
    leave the last-known values in place rather than call this at all.

    The single rule for every key below: a key that is absent or malformed
    is SKIPPED, never defaulted. A partial camera object (e.g. a read that
    only returned some fields) must not blank a state that was correct a
    moment ago, and an absent boolean (e.g. no isMicEnabled key) must not
    be reported as a confident False -- "mic disabled" is a real reading,
    not the same thing as "unknown". A non-dict nested object (a malformed
    `ledSettings`/`osdSettings`) must not raise -- it just means those keys
    are skipped too.
    """
    states = []

    camera_type = info.get("type")
    if isinstance(camera_type, str) and camera_type:
        states.append({"key": "cameraModel", "value": camera_type})

    video_mode = info.get("videoMode")
    if isinstance(video_mode, str) and video_mode:
        states.append({"key": "videoMode", "value": video_mode})

    hdr_type = info.get("hdrType")
    if isinstance(hdr_type, str) and hdr_type:
        states.append({"key": "hdrType", "value": hdr_type})

    if "isMicEnabled" in info:
        states.append({"key": "micEnabled", "value": bool(info["isMicEnabled"])})

    try:
        mic_volume = info["micVolume"]
        if isinstance(mic_volume, bool):
            # int(True) == 1 -- a real-looking but fabricated volume.
            raise TypeError("micVolume must not be a bool")
        states.append({"key": "micVolume", "value": int(mic_volume)})
    except (KeyError, TypeError, ValueError):
        # Missing, a bool, or otherwise unparseable -- skipped, not
        # defaulted to 0. 0 is a real, meaningful mic volume.
        pass

    led = info.get("ledSettings")
    if isinstance(led, dict) and "isEnabled" in led:
        states.append({"key": "ledEnabled", "value": bool(led["isEnabled"])})

    osd = info.get("osdSettings")
    if isinstance(osd, dict):
        if "isNameEnabled" in osd:
            states.append({"key": "osdNameEnabled", "value": bool(osd["isNameEnabled"])})
        if "isDateEnabled" in osd:
            states.append({"key": "osdDateEnabled", "value": bool(osd["isDateEnabled"])})

    return states


def _as_dict(value):
    """Type-safe access for a JSON sub-object that might be null, missing,
    or (a malformed/future API response) simply the wrong type. Never
    raises. Used everywhere a nested object from a poll response is read,
    e.g. `_as_dict(info.get("batteryStatus")).get("percentage")` -- unlike
    `info.get("batteryStatus", {})`, this also survives the key being
    PRESENT with a JSON `null` value, which the `.get(key, default)` form
    does not guard (the default only applies when the key is absent)."""
    return value if isinstance(value, dict) else {}


def _is_real_number(value):
    """True for a genuine int/float, explicitly excluding bool -- a bool IS
    an int subclass in Python, so `isinstance(True, int)` is True and
    `int(True) == 1` would otherwise become a real-looking but fabricated
    reading (a battery percentage, an LED level, a volume, ...), exactly
    the same trap `_camera_info_states` already guards for `micVolume`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _state_or_unavailable(info, key="state"):
    """String state derived from a poll object's own field (cameraState/
    sensorState/lightState/chimeState/armStatus): STATE_UNAVAILABLE when
    `info` itself is falsy, OR when the field is present but null/empty --
    `info.get(key, STATE_UNAVAILABLE)` only substitutes the default when
    the key is ABSENT, so a field that is present but `None` would
    otherwise write `None` into what Indigo declares as a String state."""
    if not info:
        return STATE_UNAVAILABLE
    return info.get(key) or STATE_UNAVAILABLE


def _assert_no_url_in_message(message, *value_sources):
    """Defensive guard for issue #7, mirroring protect_api.py's
    ``_assert_no_secret``: an RTSPS stream URL embeds an access token and
    must never reach the Event Log. Every log line this feature emits
    passes through here -- callers pass every dict of quality->value they
    touched (fresh values from the controller AND the device's current
    stored states), because on the error path there is no fresh response
    to check and the CURRENT states are what could leak instead.
    """
    for source in value_sources:
        for value in source.values():
            if isinstance(value, str) and value and value in message:
                raise AssertionError("a stream URL must never appear in a log message")
    return message


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs, **kwargs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("showDebugInfo", False)
        self.host = pluginPrefs.get("host", "").strip()
        self.api_key = pluginPrefs.get("apiKey", "").strip()
        self.verify_ssl = pluginPrefs.get("verifySSL", False)

        self.api = None
        self.socket = None
        self.tracker = EventTracker()

        # Issue #18: a second, independent WebSocket for device-object
        # (config/state) push, alongside the events socket above. Its own
        # retry/backoff state is deliberately separate from the reconnect
        # loop in runConcurrentThread -- a device-socket outage must never
        # be able to affect the events socket, which is the only thing
        # `connected` (and therefore motion validity) means. See
        # docs/CONTRACT.md, "second socket" for the full rationale.
        self.device_socket = None
        self.device_router = DeviceUpdateRouter()
        self._device_socket_retry_at = 0.0
        self._device_socket_backoff = BACKOFF_START
        self._device_socket_warned = False
        # F7: monotonic time the device socket last completed a successful
        # handshake. Backoff/warned are deliberately NOT reset the instant
        # connect() succeeds -- only once a connection has stayed up for
        # >= STABLE_AFTER is it trusted (see _pump_device_socket). Without
        # this, an accept-then-drop server produces a hot ~1s reconnect/
        # WARNING/INFO loop forever, the exact failure STABLE_AFTER already
        # exists to prevent on the events socket.
        self._device_socket_connected_at = None
        # Which modelKey strings we've already logged this plugin run (an
        # unhandled-but-known key at DEBUG, anything else at WARNING) --
        # same one-per-key-per-run pattern as _reported_ignored_types.
        self._reported_ignored_models = set()
        # F4: device_router.malformed_count already reported this run --
        # 0 means "never reported"; same delta-report shape as
        # _reported_dropped, but WARNING only on the first increase (a
        # firmware envelope change would otherwise flood the Event Log at
        # WARNING once per malformed frame forever).
        self._reported_device_malformed = 0
        # F8: (model_key, exception type name) already logged at ERROR this
        # plugin run -- same one-per-key pattern as _reported_ignored_types,
        # so a persistent defect on chatty NVR/etc update frames doesn't
        # flood the Event Log once per frame.
        self._device_frame_error_reported = set()

        # Protect camera id -> set of Indigo device ids. A set, not a scalar:
        # two Indigo devices pointed at one camera is trivially produced by the
        # Duplicate command, and a 1:1 dict silently freezes the loser forever.
        self.cameras = {}
        self.camera_info = {}

        # Device ids awaiting a stream-URL refresh (issue #7), drained one
        # per _pump tick. Populated cheaply (no REST) by deviceStartComm and
        # _open_socket -- see _prime_stream_urls.
        self._stream_refresh_pending = set()

        # Issue #8: one registry + one info cache per non-camera device
        # class, same dict[protect_id -> set[indigo_device_id]] shape as
        # self.cameras/self.camera_info above.
        self.sensors = {}
        self.sensor_info = {}
        self.lights = {}
        self.light_info = {}
        self.chimes = {}
        self.chime_info = {}
        # Keyed by the NVR's own id once a poll has told us it, or the
        # literal string "nvr" before that -- there is only ever one NVR.
        self.nvrs = {}
        self.nvr_info = None
        self._nvr_known_id = None
        self._protect_version = ""

        # Protect id -> epoch-ms wall-clock time of the last successful poll
        # write for that sensor/light device. Used to decide whether a live
        # pulse (sensorBatteryLow, sensorExtremeValues, lightMotion) is newer
        # than the last poll and should override the poll baseline. Captured
        # BEFORE the poll's REST request goes out (not after it returns) so a
        # pulse that arrives mid-request is never mistaken for stale, and
        # only advanced after that device's write is attempted.
        self._device_last_poll_ms = {}
        self._last_poll = 0.0
        # (class_name, exc.kind) -> currently in a reported-failure state,
        # so the ERROR log fires once per class PER FAILURE KIND per outage
        # (an auth failure and a transport failure are different problems)
        # rather than once per poll cycle. A class_name with no failing kind
        # in this set is healthy.
        self._poll_failed_classes = set()
        # (class_name, protect_id) currently reported as absent from a
        # successful list poll (still registered in Indigo, but Protect's
        # list no longer mentions it) -- so the WARNING fires once per
        # absence-episode, not once per poll cycle.
        self._absent_from_list_reported = set()

        self._last_rest_call = 0.0
        self._reconnect_requested = False
        self._reported_dropped = 0
        # Which ignored item.type strings we've already logged this plugin
        # run -- so a burst of the same unsupported/unknown type doesn't
        # spam the Event Log once per frame.
        self._reported_ignored_types = set()
        # Device ids for which a dev.model update has already logged its
        # one WARNING this plugin run -- indigo.devices.get() returns a
        # fresh object every call, so a persistent replaceOnServer()
        # failure would otherwise retry (and log at DEBUG) on every single
        # frame forever, with no visible hint anything is wrong.
        self._model_update_warned = set()
        # Resolved lazily on first use, NOT here. Indigo exec()s plugin.py as a
        # string, so __file__ does not exist and touching it in __init__ kills
        # the plugin at InitializeMain before any of it runs.
        self._snapshot_dir = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        # NB: no super().startup() -- it does not exist on PluginBase.
        self.logger.info("UniFi Protect starting")
        self._rebuild_client()
        self._sync_web_page()

    def shutdown(self):
        self.logger.info("UniFi Protect stopping")
        # Leaving devices at connected=True after a shutdown would assert a
        # health we are about to stop maintaining.
        self._safe_mark_all_disconnected()
        self._close_socket()

    def _rebuild_client(self):
        if not self.host or not self.api_key:
            self.api = None
            self.logger.warning(
                "UniFi Protect is not configured - set the host and API key in "
                "Plugins > UniFi Protect > Configure. Camera devices will report "
                "connected=false until then."
            )
            return
        self.api = ProtectAPI(self.host, self.api_key, verify_ssl=self.verify_ssl)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self.host = valuesDict.get("host", "").strip()
        self.api_key = valuesDict.get("apiKey", "").strip()
        self.verify_ssl = valuesDict.get("verifySSL", False)
        self._rebuild_client()
        # Every save re-syncs (issue #27) rather than only on an off->on
        # transition: it's order-independent (works whether Indigo has
        # already folded valuesDict into self.pluginPrefs by the time this
        # runs or not -- evidence says it has, which made a transition
        # guard here never fire), and _sync_web_page is itself a no-op
        # when the pref is off or the installed copy already matches.
        self._sync_web_page(valuesDict)
        # Do NOT close the socket here. This runs on Indigo's UI thread, and
        # ProtectEventSocket is single-threaded by contract: closing an fd that
        # runConcurrentThread is blocked reading is genuinely unsafe, not merely
        # racy. Signal instead and let the owning thread tear its own socket
        # down on its next 1s tick.
        self._reconnect_requested = True

    def validatePrefsConfigUi(self, valuesDict):
        errors = indigo.Dict()
        host = valuesDict.get("host", "").strip()
        if not host:
            errors["host"] = "Enter the IP address or hostname of your UniFi OS console."
        elif "://" in host or "/" in host:
            errors["host"] = "Enter the host only, with no https:// prefix and no path."
        if not valuesDict.get("apiKey", "").strip():
            errors["apiKey"] = "Enter an API key (UniFi OS: Settings > Control Plane > Integrations)."
        if errors:
            return False, valuesDict, errors
        return True, valuesDict

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------

    def deviceStartComm(self, dev):
        device_type = dev.deviceTypeId
        if device_type == "protectSensor":
            self._start_sensor(dev)
        elif device_type == "protectLight":
            self._start_light(dev)
        elif device_type == "protectChime":
            self._start_chime(dev)
        elif device_type == "protectNvr":
            self._start_nvr(dev)
        else:
            self._start_camera(dev)

    def _start_camera(self, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(
                f"{dev.name}: no camera selected - edit the device settings and pick one."
            )
            return
        self.cameras.setdefault(camera_id, set()).add(dev.id)
        # States declared in Devices.xml are NOT retroactively added to
        # devices created by an older plugin version — Indigo only reads
        # the state list on dialog dismissal. Without this call, every write
        # below to a state this device doesn't yet know about is silently
        # dropped with an "ignoring update request" line in the event log
        # (bit the 2026.3.0 upgrade live, 2026-08-31).
        try:
            dev.stateListOrDisplayStateIdChanged()
        except Exception as exc:
            self.logger.warning(
                f"{dev.name}: could not refresh the device state list "
                f"({type(exc).__name__}: {exc}) - states added by this "
                f"plugin version may not update until the device is re-saved"
            )
        # connected is DERIVED, never assumed. At this point in the lifecycle
        # Indigo has not yet started runConcurrentThread, so there is no socket
        # and the honest answer is False.
        self._apply_camera_state(camera_id, force=True)
        # Cheap only (issue #7) -- this runs on Indigo's main thread, and
        # _rest sleeps >= MIN_REST_INTERVAL per call, so doing REST here for
        # N opted-in cameras would block plugin startup for >= 3N seconds.
        # The actual fetch happens later via the _pump drain.
        self._prime_stream_urls(dev)

    def _start_sensor(self, dev):
        sensor_id = dev.pluginProps.get("sensorId", "")
        if not sensor_id:
            self.logger.error(
                f"{dev.name}: no sensor selected - edit the device settings and pick one."
            )
            return
        self.sensors.setdefault(sensor_id, set()).add(dev.id)
        self._apply_sensor_state(sensor_id, force=True)

    def _start_light(self, dev):
        light_id = dev.pluginProps.get("lightId", "")
        if not light_id:
            self.logger.error(
                f"{dev.name}: no light selected - edit the device settings and pick one."
            )
            return
        self.lights.setdefault(light_id, set()).add(dev.id)
        self._apply_light_state(light_id, force=True)

    def _start_chime(self, dev):
        chime_id = dev.pluginProps.get("chimeId", "")
        if not chime_id:
            self.logger.error(
                f"{dev.name}: no chime selected - edit the device settings and pick one."
            )
            return
        self.chimes.setdefault(chime_id, set()).add(dev.id)
        self._apply_chime_state(chime_id, force=True)

    def _start_nvr(self, dev):
        # There is only ever one NVR -- no id is known until the first
        # successful poll, so register under the placeholder key for now;
        # _rekey_nvr() moves this set once the real id is learned.
        key = self._nvr_known_id or "nvr"
        self.nvrs.setdefault(key, set()).add(dev.id)
        self._apply_nvr_state(force=True)

    def deviceStopComm(self, dev):
        # Remove by device id, not by whatever id is in props: if the user
        # just edited the device to point at a different camera/sensor/
        # light/chime, the props already hold the NEW id and popping by it
        # would orphan the old mapping. Every registry below shares the same
        # dict[protect_id -> set[indigo_device_id]] shape, so one loop covers
        # all five.
        for registry in (self.cameras, self.sensors, self.lights, self.chimes, self.nvrs):
            for key in list(registry):
                registry[key].discard(dev.id)
                if not registry[key]:
                    del registry[key]

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        field, label = _DEVICE_ID_FIELD_AND_LABEL.get(typeId, (None, None))
        if field is None:
            # protectNvr (or any future no-picker type): nothing to validate.
            return True, valuesDict
        if not valuesDict.get(field, "").strip():
            errors[field] = (
                f"No {label} selected. If the list was empty or showed an error, the "
                f"{label} list could not be loaded - check the Event Log, then close "
                "and reopen this dialog."
            )
            return False, valuesDict, errors
        return True, valuesDict

    def validateActionConfigUi(self, valuesDict, typeId, deviceId):
        """The one check that can't be a gate in the callback: whether the
        DIALOG's own fields make sense before anything is sent anywhere.
        Capability gating (hasLedStatus, hasHdr, ...) happens in the action
        callback instead, because it depends on the camera object, not on
        what the user typed here.
        """
        errors = indigo.Dict()
        if typeId == "setOsdOverlay":
            fields = ("showName", "showDate", "showLogo", "overlayLocation")
            if all(valuesDict.get(field, "unchanged") == "unchanged" for field in fields):
                message = "Select at least one field to change - all are Unchanged."
                for field in fields:
                    errors[field] = message
        elif typeId == "setMicVolume":
            try:
                volume = int(valuesDict.get("micVolume", ""))
            except (TypeError, ValueError):
                errors["micVolume"] = "Enter a whole number from 0 to 100."
            else:
                if not 0 <= volume <= 100:
                    errors["micVolume"] = "Must be between 0 and 100."
        elif typeId == "setVideoMode":
            if not valuesDict.get("videoMode", "").strip():
                errors["videoMode"] = "Select a video mode."
        elif typeId in ("ptzGotoPreset", "ptzPatrolStart"):
            if valuesDict.get("slot") not in PTZ_SLOTS:
                errors["slot"] = "Select a preset/patrol."
        elif typeId == "triggerAlarmWebhook":
            if not valuesDict.get("webhookId", "").strip():
                errors["webhookId"] = "Enter the Alarm Manager webhook trigger ID."
        elif typeId == "deleteStreamUrls":
            if not any(_truthy(valuesDict.get(field), default=False)
                       for field in RTSPS_DELETE_QUALITIES):
                message = "Select at least one quality to delete."
                for field in RTSPS_DELETE_QUALITIES:
                    errors[field] = message
        if errors:
            return False, valuesDict, errors
        return True, valuesDict

    def getCameraList(self, filter="", valuesDict=None, typeId="", targetId=0):
        if not self.api:
            return [("", "Plugin not configured")]
        try:
            cameras = self._rest(self.api.get_cameras)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not list cameras: {exc}")
            return [("", self._menu_error_label(exc))]
        self.camera_info = {c["id"]: c for c in cameras if c.get("id")}
        return sorted(
            ((c["id"], c.get("name") or c["id"]) for c in cameras if c.get("id")),
            key=lambda pair: pair[1].lower(),
        )

    def getSensorList(self, filter="", valuesDict=None, typeId="", targetId=0):
        if not self.api:
            return [("", "Plugin not configured")]
        try:
            sensors = self._rest(self.api.get_sensors)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not list sensors: {exc}")
            return [("", self._menu_error_label(exc))]
        self.sensor_info = {s["id"]: s for s in sensors if s.get("id")}
        return sorted(
            ((s["id"], s.get("name") or s["id"]) for s in sensors if s.get("id")),
            key=lambda pair: pair[1].lower(),
        )

    def getLightList(self, filter="", valuesDict=None, typeId="", targetId=0):
        if not self.api:
            return [("", "Plugin not configured")]
        try:
            lights = self._rest(self.api.get_lights)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not list lights: {exc}")
            return [("", self._menu_error_label(exc))]
        self.light_info = {l["id"]: l for l in lights if l.get("id")}
        return sorted(
            ((l["id"], l.get("name") or l["id"]) for l in lights if l.get("id")),
            key=lambda pair: pair[1].lower(),
        )

    def getChimeList(self, filter="", valuesDict=None, typeId="", targetId=0):
        if not self.api:
            return [("", "Plugin not configured")]
        try:
            chimes = self._rest(self.api.get_chimes)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not list chimes: {exc}")
            return [("", self._menu_error_label(exc))]
        self.chime_info = {c["id"]: c for c in chimes if c.get("id")}
        return sorted(
            ((c["id"], c.get("name") or c["id"]) for c in chimes if c.get("id")),
            key=lambda pair: pair[1].lower(),
        )

    @staticmethod
    def _menu_error_label(exc):
        """A rate limit is transient and retryable; a bad key is not. Saying so
        in the menu itself is the difference between a user retrying and a user
        filing a bug."""
        kind = getattr(exc, "kind", None)
        if kind == "rate_limited":
            return "Rate limited - wait a few seconds and reopen"
        if kind == "auth":
            return "API key rejected - check the plugin config"
        return "Error - see the Event Log"

    def getVideoModeList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Video mode menu for setVideoMode's ConfigUI, scoped to the target
        device's actual camera capability (issue #6). When the camera isn't
        cached yet -- a brand new device, or no GET /cameras since plugin
        start -- falls back to Protect's published OpenAPI enum with each
        label marked "(unverified)" so the user isn't misled into thinking
        every listed mode is confirmed to work on their hardware. The PATCH
        itself is still validated against the camera's real list in
        setVideoMode, so a stale/unverified pick here is refused there, not
        silently accepted.
        """
        camera_id = self._camera_id_for_target(targetId)
        info = self.camera_info.get(camera_id) if camera_id else None
        modes = (info.get("featureFlags") or {}).get("videoModes") if info else None
        if modes:
            return [(mode, mode) for mode in modes]
        return [(mode, f"{mode} (unverified)") for mode in VIDEO_MODE_SPEC_ENUM]

    def _camera_id_for_target(self, target_id):
        """Resolve an Indigo device id (a dynamic-list method's targetId) to
        the Protect camera id it's configured for. Returns None if the
        device doesn't exist, hasn't been assigned a camera yet, or simply
        has no `cameraId` key in its pluginProps at all -- this method does
        NOT check the device's plugin id; a device belonging to a different
        plugin degrades harmlessly through that last case instead, since it
        has no such key either. Callers treat every case above as
        "unknown"."""
        if not target_id:
            return None
        dev = indigo.devices.get(target_id, None)
        if dev is None:
            return None
        return dev.pluginProps.get("cameraId", "") or None

    # ------------------------------------------------------------------
    # Event socket
    # ------------------------------------------------------------------

    def runConcurrentThread(self):
        backoff = BACKOFF_START
        try:
            while True:
                if not self.api:
                    # Unconfigured is not healthy. Without this the devices sit
                    # at connected=True forever, which is the exact lie this
                    # plugin's docstring promises not to tell.
                    self._safe_mark_all_disconnected()
                    self.sleep(10)
                    continue

                connected_at = None
                try:
                    self._open_socket()
                    connected_at = time.monotonic()
                    self._pump()
                except self.StopThread:
                    # StopThread subclasses Exception, so without this it lands
                    # in the generic handler below and every shutdown logs a
                    # contentless "Event socket error".
                    raise
                except ConnectionError as exc:
                    self.logger.warning(f"Event socket lost ({exc}); reconnecting in {backoff:.0f}s")
                except ProtectAPIError as exc:
                    if getattr(exc, "kind", None) == "auth":
                        # Permanent and user-fixable. Retrying at 60s forever
                        # while logging "lost" implies a network fault and
                        # never tells the user what is actually wrong.
                        self.logger.error(
                            "UniFi Protect rejected the API key. Regenerate it in UniFi OS "
                            "(Settings > Control Plane > Integrations) and update the plugin "
                            "config. Not retrying until the config changes."
                        )
                        self._safe_mark_all_disconnected()
                        self._close_socket()
                        self._wait_for_reconfigure()
                        backoff = BACKOFF_START
                        continue
                    self.logger.error(f"Protect API error on connect: {exc}")
                except Exception as exc:
                    self.logger.error(f"Event socket error: {type(exc).__name__}: {exc}")
                    self.logger.debug("event socket traceback", exc_info=True)

                # Outside the inner try on purpose, but wrapped: a failure here
                # must never kill runConcurrentThread, because Indigo does not
                # restart it and the plugin would still report itself Running.
                self._safe_mark_all_disconnected()
                self._close_socket()

                if connected_at and (time.monotonic() - connected_at) >= STABLE_AFTER:
                    backoff = BACKOFF_START
                self.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        except self.StopThread:
            self._safe_mark_all_disconnected()
            self._close_socket()

    def _wait_for_reconfigure(self):
        """Idle until the user changes the config, checking for shutdown."""
        while not self._reconnect_requested:
            self.sleep(5)
        self._reconnect_requested = False

    def _open_socket(self):
        sock = ProtectEventSocket(
            self.host, self.api_key, verify_ssl=self.verify_ssl, logger=self.logger
        )
        # Only publish it once the handshake has succeeded. If connect() raises,
        # self.socket stays None and _is_connected() cannot report a half-built
        # socket as healthy.
        sock.connect()
        self.socket = sock
        self.tracker.reset()
        self.logger.info("Event socket connected")
        self._refresh_camera_info()
        for camera_id in list(self.cameras):
            self._apply_camera_state(camera_id, force=True)
            # Cheap only (issue #7), same reasoning as deviceStartComm --
            # REST happens later via the _pump drain, never here, or it
            # would delay _pump's time-to-first-frame by >= 3s per
            # opted-in camera on every single reconnect.
            for dev_id in sorted(self.cameras.get(camera_id, ())):
                dev = indigo.devices.get(dev_id, None)
                if dev is not None and dev.enabled:
                    self._prime_stream_urls(dev)
        # Issue #8: re-apply connected=True immediately for every
        # registered sensor/light/chime/NVR device, regardless of whether
        # the poll below succeeds. Without this, a failing first poll after
        # reconnect leaves these devices reporting connected=False forever
        # (or until a poll eventually succeeds), even though the event
        # socket -- what `connected` actually means for every device class
        # in this plugin -- is genuinely back up. _write_*_states already
        # writes only the lifecycle booleans/connected/*State=unavailable
        # when nothing is cached yet, so this is safe to call unconditionally.
        for sensor_id in list(self.sensors):
            self._apply_sensor_state(sensor_id, connected=True, force=True)
        for light_id in list(self.lights):
            self._apply_light_state(light_id, connected=True, force=True)
        for chime_id in list(self.chimes):
            self._apply_chime_state(chime_id, connected=True, force=True)
        if self.nvrs:
            self._apply_nvr_state(connected=True, force=True)
        # Only poll if at least one non-camera device is registered -- an
        # API that raises if touched (proved by a fatal-collaborator test)
        # must never be touched when only cameras exist.
        if self.sensors or self.lights or self.chimes or self.nvrs:
            self._poll_devices()

        self._open_device_socket()

    def _open_device_socket(self):
        """Best-effort connect for the /subscribe/devices socket (issue
        #18). Failure here must NEVER be treated like an events-socket
        failure -- it does not raise, does not touch `connected`, and does
        not touch the tracker. On failure it just sets a monotonic retry
        gate (exponential backoff, same shape as the events socket's) and
        leaves self.device_socket None; _pump() retries once that gate
        passes.
        """
        try:
            sock = ProtectEventSocket(
                self.host, self.api_key, verify_ssl=self.verify_ssl, logger=self.logger,
                path=DEVICE_SOCKET_PATH, label="device",
            )
            sock.connect()
        except ConnectionError as exc:
            self._note_device_socket_connect_failure(str(exc))
            return
        except Exception as exc:  # pylint: disable=broad-except
            # F5: mirrors device_router.route()'s belt-and-braces
            # rationale -- protect_ws's deliberate _assert_no_secret
            # AssertionError, or a future struct.error in the shared frame
            # parser, must never escape here and be mistaken for an
            # EVENTS-socket fault by runConcurrentThread's generic handler.
            # Contain and surface, don't crash the motion feed.
            self.logger.debug("device socket connect traceback", exc_info=True)
            self._note_device_socket_connect_failure(f"{type(exc).__name__}: {exc}")
            return

        self.device_socket = sock
        # F7: do NOT reset backoff/warned here -- a connection is only
        # trusted (forgiven) once it has stayed up for >= STABLE_AFTER; see
        # _pump_device_socket. Resetting on every successful handshake is
        # exactly what let an accept-then-drop server produce a hot ~1s
        # reconnect/WARNING/INFO loop forever.
        self._device_socket_connected_at = time.monotonic()
        if not self._device_socket_warned:
            self.logger.info("Device-update socket connected")
        else:
            # Mid-flap reconnect -- still within an outage that already
            # warned once. Must not spam INFO on every retry.
            self.logger.debug("Device-update socket connected (not yet stable)")

    def _note_device_socket_connect_failure(self, reason):
        """Shared warn-once/backoff bookkeeping for a failed connect
        attempt, used by both the ConnectionError and generic-Exception
        branches of _open_device_socket (F5) so they share one outage
        counter/message shape."""
        if not self._device_socket_warned:
            self._device_socket_warned = True
            self.logger.warning(
                f"Device-update socket could not connect ({reason}); falling back to "
                "60s polling for config/state freshness - camera motion is unaffected"
            )
        self._device_socket_retry_at = time.monotonic() + self._device_socket_backoff
        self._device_socket_backoff = min(self._device_socket_backoff * 2, BACKOFF_MAX)

    def _pump(self):
        last_ping = time.monotonic()
        last_device_ping = time.monotonic()
        while True:
            # Only self.sleep() raises StopThread, and this loop blocks in
            # recv() rather than sleeping -- so check the flag explicitly or a
            # shutdown hangs until the socket happens to die.
            if self.stopThread:
                raise self.StopThread
            if self._reconnect_requested:
                self._reconnect_requested = False
                raise ConnectionError("reconnect requested after a configuration change")

            # Read timeout halved (was 1.0) so the combined per-tick budget
            # with the device-socket read below stays ~1s -- shutdown/
            # reconnect responsiveness is unchanged.
            message = self.socket.read_message(timeout=0.5)
            now = time.monotonic()

            if message is not None:
                changed = self.tracker.handle(message)
                for device_id in changed:
                    # A device id belongs to at most one registry in
                    # practice (Protect ids are UUIDs, not shared across
                    # cameras/sensors/lights), but checking all three is
                    # harmless and avoids depending on that.
                    if device_id in self.cameras:
                        self._apply_camera_state(device_id)
                    if device_id in self.sensors:
                        self._apply_sensor_state(device_id)
                    if device_id in self.lights:
                        self._apply_light_state(device_id)
                self._report_dropped_frames()
                self._report_ignored_types()

            # One pending stream-URL refresh per tick (issue #7). REST-
            # throttled by _rest, so N pending devices drain at one per
            # ~MIN_REST_INTERVAL without ever gating socket readiness.
            self._drain_one_pending_stream_refresh()

            # Issue #8: sensors/lights/chimes/NVR are REST-polled, not
            # pushed -- chimes and the NVR have no event-socket feed at all,
            # and sensors/lights still need a periodic poll for their
            # measurements/config even though some of their state is
            # pulse/lifecycle-driven above. Each polled class costs one
            # throttled REST call (_rest enforces MIN_REST_INTERVAL=3s), so
            # a full poll cycle here can block this loop for up to N*3s;
            # any WS frames that arrive meanwhile simply queue in the
            # socket's own read buffer -- an accepted trade against running
            # a second thread just for polling.
            if now - self._last_poll >= DEVICE_POLL_INTERVAL:
                self._poll_devices()

            # Active liveness probe. See PING_INTERVAL above for why silence
            # alone cannot be trusted as a death signal.
            if now - last_ping >= PING_INTERVAL:
                self.socket.send_ping()
                last_ping = now
            if now - self.socket.last_frame_at > STALE_TIMEOUT:
                raise ConnectionError(
                    f"no frame of any kind for {STALE_TIMEOUT:.0f}s despite pings - "
                    "treating the socket as dead"
                )

            last_device_ping = self._pump_device_socket(now, last_device_ping)

    def _pump_device_socket(self, now, last_device_ping):
        """One tick of the /subscribe/devices socket (issue #18).

        Entirely independent of the events socket handled above: any
        failure here is caught, logged at most once per outage, and re-gated
        with its own backoff -- it must NEVER raise out of _pump (that would
        be mistaken for an events-socket fault and tear the motion feed
        down), and must never touch `connected` or the tracker. Returns the
        (possibly updated) last-ping timestamp for the caller to carry into
        the next tick.
        """
        if self.device_socket is None:
            if self.api and now >= self._device_socket_retry_at:
                self._open_device_socket()
                return now
            return last_device_ping

        try:
            message = self.device_socket.read_message(timeout=0.5)
        except ConnectionError as exc:
            self._fail_device_socket(f"Device-update socket lost ({exc})")
            return last_device_ping
        except Exception as exc:  # pylint: disable=broad-except
            # F5: same containment as _open_device_socket's connect -- a
            # non-ConnectionError defect here (e.g. protect_ws's
            # _assert_no_secret AssertionError) must never escape and be
            # mistaken for an events-socket fault.
            self.logger.debug("device socket read traceback", exc_info=True)
            self._fail_device_socket(f"Device-update socket lost ({type(exc).__name__}: {exc})")
            return last_device_ping

        if message is not None:
            routed = self.device_router.route(message)
            if routed is None:
                self._report_ignored_models()
                self._report_malformed_device_frames()
            else:
                self._handle_device_frame(*routed)

        if self.device_socket is None:
            return last_device_ping

        # F7: a connection is only forgiven (backoff/warned reset) once it
        # has stayed up for >= STABLE_AFTER -- this is the one place that
        # forgiveness happens. Without it, an accept-then-drop server would
        # warn/reconnect in a hot ~1s loop forever, since _open_device_socket
        # itself deliberately no longer resets on a bare successful connect.
        if (self._device_socket_warned and self._device_socket_connected_at is not None
                and now - self._device_socket_connected_at >= STABLE_AFTER):
            self._device_socket_warned = False
            self._device_socket_backoff = BACKOFF_START
            self.logger.info("Device-update socket recovered")

        if now - last_device_ping >= PING_INTERVAL:
            try:
                self.device_socket.send_ping()
            except ConnectionError as exc:
                self._fail_device_socket(f"Device-update socket lost ({exc})")
                return last_device_ping
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.debug("device socket ping traceback", exc_info=True)
                self._fail_device_socket(
                    f"Device-update socket lost ({type(exc).__name__}: {exc})")
                return last_device_ping
            last_device_ping = now
        elif now - self.device_socket.last_frame_at > STALE_TIMEOUT:
            self._fail_device_socket(
                f"Device-update socket looks dead (no frame for {STALE_TIMEOUT:.0f}s "
                "despite pings)"
            )
        return last_device_ping

    def _fail_device_socket(self, reason):
        """Tear the device socket down after a read/ping failure or a
        staleness timeout -- same containment and one-per-outage WARNING as
        _open_device_socket's connect failure (both toggle
        _device_socket_warned), just a different message for a socket that
        WAS working rather than one that never connected."""
        try:
            self.device_socket.close()
        except Exception:  # pylint: disable=broad-except
            pass
        self.device_socket = None
        if not self._device_socket_warned:
            self._device_socket_warned = True
            self.logger.warning(
                f"{reason}; falling back to 60s polling for config/state freshness - "
                "camera motion is unaffected"
            )
        self._device_socket_retry_at = time.monotonic() + self._device_socket_backoff
        self._device_socket_backoff = min(self._device_socket_backoff * 2, BACKOFF_MAX)

    def _report_ignored_models(self):
        """Surface the device router's ignore-and-count for modelKey values
        this plugin has no Indigo device type for, once per modelKey per
        plugin run -- mirrors _report_ignored_types. A documented-but-
        unhandled key (viewer, speaker, bridge, ...) is expected and
        unremarkable -- DEBUG. Anything else is either a genuinely new
        modelKey or a parsing gap -- WARNING."""
        for model_key in self.device_router.ignored_model_counts:
            if model_key in self._reported_ignored_models:
                continue
            self._reported_ignored_models.add(model_key)
            if model_key == MISSING_MODEL_KEY:
                what = "device frames with no `modelKey` field"
            else:
                what = f"'{model_key}' device frames"
            if model_key in KNOWN_UNHANDLED_MODEL_KEYS:
                self.logger.debug(f"Ignoring {what} on the device socket - not supported yet")
            else:
                self.logger.warning(
                    f"Ignoring {what} on the device socket - unrecognized modelKey. "
                    "Please report this on GitHub with a debug capture."
                )

    def _report_malformed_device_frames(self):
        """F4: surface device_router's malformed_count, mirroring
        _report_dropped_frames -- without this, a firmware envelope change
        could silently kill the whole /subscribe/devices feature while the
        socket looks healthy (frames still refresh last_frame_at). WARNING
        on the first increase this plugin run; every increase after that
        logs at DEBUG so a persistently malformed stream doesn't flood the
        Event Log at WARNING once per frame forever."""
        malformed = self.device_router.malformed_count
        if malformed <= self._reported_device_malformed:
            return
        delta = malformed - self._reported_device_malformed
        first_time = self._reported_device_malformed == 0
        self._reported_device_malformed = malformed
        if first_time:
            self.logger.warning(
                f"Discarding {delta} unparseable device-socket frame(s) - the "
                "device-config push may be broken; sensor/light/chime/NVR polling "
                "still applies and cameras fall back to 60s refresh"
            )
        else:
            self.logger.debug(f"Discarding {delta} more unparseable device-socket frame(s)")

    # -- Issue #18: applying routed /subscribe/devices frames -------------

    def _handle_device_frame(self, kind, model_key, device_id, item):
        """Apply one routed /subscribe/devices frame to the matching cache
        + Indigo state.

        Wrapped entirely in try/except: a defect in this NEW speculative
        path must never be able to discard a result the OLD reliable
        motion path already produced, or take the event socket down --
        mirrors the containment `_drain_one_pending_stream_refresh` and
        `_apply_polled_write` already have for their own per-frame/per-poll
        work.
        """
        try:
            if model_key == "nvr":
                self._handle_nvr_device_frame(kind, device_id, item)
                return

            registry, cache, apply_fn = {
                "camera": (self.cameras, self.camera_info, self._apply_camera_state),
                "sensor": (self.sensors, self.sensor_info, self._apply_sensor_state),
                "light": (self.lights, self.light_info, self._apply_light_state),
                "chime": (self.chimes, self.chime_info, self._apply_chime_state),
            }[model_key]

            if kind == "update":
                # Merging onto nothing would fabricate a partial object
                # that _write_*_states would then treat as a full,
                # confirmed read -- an uncached id is ignored outright, not
                # seeded from a partial frame.
                if device_id not in cache:
                    self.logger.debug(
                        f"{model_key} {device_id}: ignoring a device-socket update for an "
                        "id with no cached object yet"
                    )
                    return
                cache[device_id] = merge_update(cache[device_id], item)
                apply_fn(device_id)
            elif kind == "add":
                # add is a FULL object per spec, unlike update.
                cache[device_id] = item
                # F2: a remove->add->remove sequence must warn on BOTH
                # removes, not just the first -- cameras have no list poll
                # to clear this episode the way sensors/lights/chimes
                # self-heal within 60s, so an `add` is the only signal that
                # the absence is over. Harmless no-op if it was never set.
                self._clear_absent_from_list(model_key, device_id)
                if device_id in registry:
                    apply_fn(device_id)
                else:
                    self.logger.debug(f"new {model_key} appeared on the controller: {device_id}")
            elif kind == "remove":
                cache.pop(device_id, None)
                if device_id in registry:
                    self._warn_absent_from_list(model_key, device_id, registry)
                    apply_fn(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            # F8: once per (model_key, exception type) per plugin run --
            # otherwise a persistent defect on chatty update frames (e.g.
            # NVR) floods the Event Log with one ERROR per frame forever.
            error_key = (model_key, type(exc).__name__)
            message = (
                f"Could not apply device-socket {kind} frame for {model_key} {device_id} "
                f"({type(exc).__name__}: {exc}) - the event socket is unaffected"
            )
            if error_key in self._device_frame_error_reported:
                self.logger.debug(message)
            else:
                self._device_frame_error_reported.add(error_key)
                self.logger.error(message)
            self.logger.debug("device-socket frame traceback", exc_info=True)

    def _handle_nvr_device_frame(self, kind, device_id, item):
        """NVR special case: `self.nvr_info` is a single dict, not a
        dict-by-id cache, and `_apply_nvr_state()` takes no id argument --
        it looks its own registered devices up via `self._nvr_known_id or
        "nvr"`, mirroring `_poll_nvr`/`_rekey_nvr`. Called only from
        `_handle_device_frame`, which already provides the try/except
        containment."""
        if kind == "update":
            # "Ids match" honours _nvr_known_id too, not just nvr_info's own
            # id -- there is exactly one NVR, so the two agree once either
            # a poll or an earlier add frame has told us the real id, but
            # relying on nvr_info alone would miss that this IS the known
            # NVR the moment _rekey_nvr has run without nvr_info yet
            # reflecting it (not reachable today, but a single source of
            # truth for "is this the NVR we know about" is cheap and safer
            # than two that could disagree).
            if self.nvr_info is None or device_id not in (
                    self.nvr_info.get("id"), self._nvr_known_id):
                self.logger.debug(
                    f"nvr {device_id}: ignoring a device-socket update - no cached NVR "
                    "with that id yet"
                )
                return
            self.nvr_info = merge_update(self.nvr_info, item)
            self._apply_nvr_state()
        elif kind == "add":
            self.nvr_info = item
            self._rekey_nvr(device_id)
            registry_key = self._nvr_known_id or "nvr"
            # F3: mirrors _handle_device_frame's clear-on-add (F2) -- an
            # add is the only signal a prior remove's absence episode is
            # over, since the NVR has no list poll of its own to clear it.
            self._clear_absent_from_list("nvr", registry_key)
            if self.nvrs.get(registry_key):
                self._apply_nvr_state()
            else:
                self.logger.debug(f"new nvr appeared on the controller: {device_id}")
        elif kind == "remove":
            # F3: an NVR remove was previously silent -- warn once per
            # absence episode, reusing _warn_absent_from_list's registry/key
            # mechanics exactly like every other class (self.nvrs is keyed
            # by _nvr_known_id, falling back to the "nvr" placeholder before
            # the first successful poll/add has told us the real id).
            registry_key = self._nvr_known_id or "nvr"
            if self.nvrs.get(registry_key):
                self._warn_absent_from_list("nvr", registry_key, self.nvrs)
            self.nvr_info = None
            self._apply_nvr_state()

    def _report_dropped_frames(self):
        """Surface the tracker's swallow. A destroyed frame that carried an
        `end` means we may have lost an active event, which is not the same as
        'nothing happened' and must not look like it."""
        dropped = getattr(self.tracker, "dropped_terminal_count", 0)
        if dropped > self._reported_dropped:
            self.logger.warning(
                f"Discarded {dropped - self._reported_dropped} unparseable event "
                "frame(s) carrying an end marker - a camera may be stuck showing motion."
            )
            self._reported_dropped = dropped

    def _report_ignored_types(self):
        """Surface the tracker's ignore-and-count for item.type values this
        plugin does not act on, once per type per plugin run so a burst of
        the same type doesn't spam the Event Log. A documented-but-not-yet-
        supported type (doorbell ring, Protect sensor) is expected and
        unremarkable -- log it at DEBUG. Anything else is either a genuinely
        new event type or a parsing gap and is worth a bug report -- log it
        at WARNING. Both include the first camera id seen sending it, so the
        log line points somewhere useful."""
        for event_type in self.tracker.ignored_type_counts:
            if event_type in self._reported_ignored_types:
                continue
            self._reported_ignored_types.add(event_type)
            sample_device = self.tracker.ignored_type_samples.get(event_type, "unknown")
            if event_type == MISSING_TYPE_KEY:
                what = "event frames with no `type` field"
            else:
                what = f"'{event_type}' event frames"
            if event_type in KNOWN_UNSUPPORTED_EVENT_TYPES:
                self.logger.debug(f"Ignoring {what} - not supported yet (e.g. device {sample_device})")
            else:
                self.logger.warning(
                    f"Ignoring {what} - not treated as motion (e.g. device {sample_device}). "
                    "Please report this on GitHub with a debug capture."
                )

    def _close_socket(self):
        # Also tears down the device socket (issue #18) -- both sockets are
        # opened together by _open_socket, so they are closed together here
        # too. The device socket's own health never factors into whether
        # THIS method needs to run; it is simply along for the ride.
        if self.device_socket is not None:
            try:
                self.device_socket.close()
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.warning(f"Error closing device socket: {type(exc).__name__}: {exc}")
            finally:
                self.device_socket = None

        if self.socket is None:
            return
        try:
            self.socket.close()
        except Exception as exc:
            # ProtectEventSocket.close() already handles every expected failure,
            # so anything reaching here is a genuine bug. Swallowing it silently
            # would also hide a leaked file descriptor.
            self.logger.warning(f"Error closing event socket: {type(exc).__name__}: {exc}")
        finally:
            self.socket = None

    def _safe_mark_all_disconnected(self):
        try:
            self._mark_all_disconnected()
        except Exception as exc:
            self.logger.error(f"Could not mark cameras disconnected: {exc}")
            self.logger.debug("mark-disconnected traceback", exc_info=True)

    def _mark_all_disconnected(self):
        """A dead socket means motion is unknown. Indigo booleans cannot say
        that, so motion goes False and `connected` goes False alongside it --
        the same forced-False rule covers `audioDetected` and the four
        specific audio states too, for the same reason. Automations are
        expected to gate on `connected`.

        Issue #8: extended to sensors/lights/chimes/NVR. Sensor lifecycle
        booleans (motion/leak/alarm/tamper) and a light's pirMotionDetected
        follow the exact same rule -- they are live-event-driven, so a dead
        socket makes them unknown too. Everything else on those device
        classes (isOpen, batteryLow, temperature, isLightOn, chime/NVR
        fields, ...) is REST-poll-derived and is deliberately left alone
        here, the same way a camera's cameraModel/videoMode survive a
        socket loss -- poll-derived values are kept, not fabricated.

        Issue #18: the device socket is deliberately NOT part of this
        method's definition of "disconnected". `connected` here means the
        EVENTS socket only -- that is the one signal motion validity
        depends on. The device socket only affects the freshness of
        poll-derived config/state between polls; losing it degrades
        gracefully to the existing 60s poll cadence and must never make a
        healthy events socket look unhealthy.
        """
        for camera_id in list(self.cameras):
            self.tracker.clear_camera(camera_id)
            self._apply_camera_state(camera_id, connected=False, force=True)
        for sensor_id in list(self.sensors):
            for family in (FAMILY_SENSOR_MOTION, FAMILY_SENSOR_LEAK,
                           FAMILY_SENSOR_ALARM, FAMILY_SENSOR_TAMPER):
                self.tracker.clear_family(family, sensor_id)
            self._apply_sensor_state(sensor_id, connected=False, force=True)
        for light_id in list(self.lights):
            self._apply_light_state(light_id, connected=False, force=True)
        for chime_id in list(self.chimes):
            self._apply_chime_state(chime_id, connected=False, force=True)
        if self.nvrs:
            self._apply_nvr_state(connected=False, force=True)

    # ------------------------------------------------------------------
    # State writing
    # ------------------------------------------------------------------

    def _is_connected(self):
        # self.socket is only ever set after a successful handshake (_open_socket)
        # and is cleared by _close_socket, so its presence IS connectedness.
        # Deliberately the EVENTS socket only -- issue #18's device socket
        # (self.device_socket) is never consulted here. Motion validity
        # depends solely on the events socket; the device socket only feeds
        # freshness of polled config/state and has its own independent
        # retry/backoff, so folding it into `connected` would make a
        # healthy motion feed report itself unhealthy over an unrelated
        # freshness-only outage.
        return self.socket is not None

    def _apply_camera_state(self, camera_id, connected=None, force=False):
        # connected defaults to None, not True: a default of True means every
        # present and future caller that forgets the kwarg silently asserts
        # health it has not checked.
        if connected is None:
            connected = self._is_connected()

        for dev_id in sorted(self.cameras.get(camera_id, ())):
            dev = indigo.devices.get(dev_id, None)
            if dev is None:
                self.logger.debug(f"stale mapping: device {dev_id} no longer exists")
                continue
            if not dev.enabled:
                continue
            self._write_states(dev, camera_id, connected, force)

    def _write_states(self, dev, camera_id, connected, force):
        motion_active = self.tracker.is_active(camera_id) if connected else False
        motion_types = sorted(self.tracker.detect_types(camera_id)) if connected else []
        audio_active = self.tracker.audio_active(camera_id) if connected else False
        audio_types = sorted(self.tracker.audio_types(camera_id)) if connected else []
        info = self.camera_info.get(camera_id)

        # Speech/baby-cry count toward onOffState by default -- the per-
        # device checkbox can opt out. Smoke/CO alarm sounds NEVER count,
        # checkbox or not: an alarm is not presence, and folding it into
        # onOffState would make a "device turned on" trigger fire on a smoke
        # alarm, burying a real alert under a routine motion notification.
        counts_as_activity = _truthy(dev.pluginProps.get("audioCountsAsActivity"))
        audio_presence = bool(counts_as_activity and PRESENCE_AUDIO_TYPES.intersection(audio_types))
        on_state = motion_active or audio_presence

        states = [
            {"key": "onOffState", "value": on_state},
            {"key": "motionDetected", "value": motion_active},
            {"key": "lastDetectTypes", "value": ",".join(motion_types)},
            {"key": "audioDetected", "value": audio_active},
            {"key": "lastAudioTypes", "value": ",".join(audio_types)},
            {"key": "cameraState", "value": info.get("state", "") if info else STATE_UNAVAILABLE},
            {"key": "connected", "value": connected},
        ]
        for detect_type in TRACKED_DETECT_TYPES:
            states.append({"key": f"{detect_type}Detected", "value": detect_type in motion_types})
        for audio_type, state_key in TRACKED_AUDIO_TYPES.items():
            states.append({"key": state_key, "value": audio_type in audio_types})

        # Hardware/config states (issue #4) come straight from the cached
        # camera object, not the WS tracker. When info is None the lookup
        # itself failed -- cameraState already says STATE_UNAVAILABLE above,
        # and leaving these eight states at their last-known values is more
        # honest than overwriting real hardware state with a made-up
        # False/"" that looks like a fresh, confirmed read.
        if info:
            states.extend(_camera_info_states(info))

        last_ms = self.tracker.last_motion_ms(camera_id)
        if isinstance(last_ms, (int, float)) and last_ms > 0:
            states.append({
                "key": "lastMotion",
                "value": datetime.fromtimestamp(last_ms / 1000.0).isoformat(timespec="seconds"),
            })

        last_audio_ms = self.tracker.last_audio_ms(camera_id)
        if isinstance(last_audio_ms, (int, float)) and last_audio_ms > 0:
            states.append({
                "key": "lastAudio",
                "value": datetime.fromtimestamp(last_audio_ms / 1000.0).isoformat(timespec="seconds"),
            })

        # The state image tracks onOffState, not motion alone -- a speech or
        # baby-cry event (with the checkbox on) trips it exactly like motion.
        if force or on_state != bool(dev.states.get("onOffState", False)):
            dev.updateStateImageOnServer(
                indigo.kStateImageSel.MotionSensorTripped if on_state
                else indigo.kStateImageSel.MotionSensor
            )
        dev.updateStatesOnServer(states)

        # Model update comes AFTER the state write on purpose: a
        # replaceOnServer() failure below must never be able to block the
        # states above from landing.
        if info:
            self._update_camera_model(dev, info)

    def _update_camera_model(self, dev, info):
        """Set the Indigo device's model from the camera's hardware type
        (issue #4), so the device list shows e.g. "UVC G5 Turret Ultra"
        instead of the generic "Protect Camera". Best-effort only -- caught
        broadly because a failure here is cosmetic, not a reason to lose the
        state write that already happened above.

        In real Indigo, indigo.devices.get() returns a FRESH device object
        on every call, so a persistent failure here (e.g. the user has this
        device's edit dialog open) would otherwise retry -- and log --
        forever with no visible trace, because DEBUG is off by default.
        Log the first failure per device at WARNING, everything after that
        at DEBUG, so a persistent fault is seen once instead of an Event
        Log flooded with one line per frame.
        """
        model = info.get("type")
        if not model or model == dev.model:
            return
        try:
            dev.model = model
            dev.replaceOnServer()
        except Exception as exc:
            if dev.id in self._model_update_warned:
                self.logger.debug(f"{dev.name}: could not update device model: {exc}")
                return
            self._model_update_warned.add(dev.id)
            self.logger.warning(
                f"{dev.name}: could not update device model to '{model}' "
                f"({type(exc).__name__}: {exc}). If this repeats, check whether "
                "this device's edit dialog is open in the Indigo UI."
            )

    # ------------------------------------------------------------------
    # Issue #8: sensors/lights/chimes/NVR polling.
    #
    # Spec-derived (OpenAPI v6.2.83) -- UNVERIFIED against real hardware,
    # since the reference rig's /sensors, /lights, /chimes all return [].
    # /nvrs is the one endpoint with real live data (armMode et al).
    #
    # Reconciliation model, per docs/CONTRACT.md: a poll's own boolean
    # fields (isMotionDetected, isPirMotionDetected) are authoritative at
    # the moment of the poll -- if a poll says motion is over while the
    # tracker still thinks it is active, the tracker is wrong and gets
    # corrected (clear_family). Between polls, a live pulse (sensorOpened/
    # Closed/BatteryLow/ExtremeValues, lightMotion) overrides the poll's
    # own value only when the pulse's `start` is newer than whatever
    # timestamp the poll itself provided (openStatusChangedAt) or, where
    # the poll has no such field, newer than `_device_last_poll_ms` -- the
    # wall-clock time of the last poll write for that device. This makes
    # every pulse override self-expiring: the NEXT poll always re-baselines.
    # ------------------------------------------------------------------

    def _poll_devices(self):
        """Poll every registered non-camera device class over REST. Only
        classes with at least one registered Indigo device are fetched --
        an API that raises if touched must never be touched when only
        cameras exist (see docs/CONTRACT.md for the fatal-collaborator test
        that pins this)."""
        self._last_poll = time.monotonic()
        if not self.api:
            return
        if self.sensors:
            self._poll_sensors()
        if self.lights:
            self._poll_lights()
        if self.chimes:
            self._poll_chimes()
        if self.nvrs:
            self._poll_nvr()
        # F1: the device-socket-down WARNINGs (_open_device_socket,
        # _fail_device_socket) promise "falling back to 60s polling for
        # config/state freshness" -- a promise that must hold for EVERY
        # class the device socket normally pushes, cameras included.
        # Without this, camera config states (ledEnabled/videoMode/
        # hdrType/micVolume/osd*) would go stale INDEFINITELY during a
        # device-socket-only outage, since nothing else in this poll cycle
        # ever touches camera_info. Only runs while the device socket is
        # actually down -- when it's up, push already covers cameras and
        # this REST call would be pure waste.
        if self.cameras and self.device_socket is None:
            if self._refresh_camera_info():
                for camera_id in list(self.cameras):
                    # Same containment as _apply_polled_write's per-device
                    # writes below (issue #8's own rule: "a poll bug must
                    # never be able to kill the motion socket") -- a write
                    # failure for one camera here must not escape into
                    # _pump/runConcurrentThread, which would tear the event
                    # socket down over an unrelated camera-state bug.
                    try:
                        self._apply_camera_state(camera_id)
                    except Exception as exc:  # pylint: disable=broad-except
                        self.logger.error(
                            f"Could not apply camera {camera_id} state during the "
                            f"device-socket-down fallback poll ({type(exc).__name__}: "
                            f"{exc}) - the event socket is unaffected"
                        )
                        self.logger.debug("camera fallback poll traceback", exc_info=True)

    def _report_poll_failure(self, class_name, exc):
        """ERROR once per (class, failure kind) per outage; DEBUG for every
        failure after the first, so a controller stuck down doesn't spam
        the Event Log once a minute forever. Keyed on `exc.kind` (falling
        back to the exception's type name for a non-ProtectAPIError) as
        well as the class, so e.g. an auth failure and a transport failure
        are each reported once rather than the second masking the first."""
        kind = getattr(exc, "kind", None) or type(exc).__name__
        key = (class_name, kind)
        # _describe_api_error (camera-control error messaging) reads
        # exc.kind/exc.status/exc.retry_after/exc.issues -- all
        # ProtectAPIError-only attributes. A non-ProtectAPIError reaching
        # here (a bug, or a fake in a test) must not raise AttributeError
        # out of a poll-failure handler, so it gets the plain fallback
        # instead of that richer formatting.
        description = (self._describe_api_error(exc, entity=class_name) if isinstance(exc, ProtectAPIError)
                        else f"{type(exc).__name__}: {exc}")
        if key not in self._poll_failed_classes:
            self._poll_failed_classes.add(key)
            self.logger.error(
                f"Could not poll {class_name}s ({description}) - last-known values are "
                "held, and the camera event socket is unaffected."
            )
        else:
            self.logger.debug(f"Could not poll {class_name}s (still failing): {description}")

    def _clear_poll_failure(self, class_name):
        """Clears every failure-kind guard for this class and logs INFO
        once if it was actually failing -- a poll (or a successful
        single-device RequestStatus) recovering is worth knowing about the
        same way the failure itself was."""
        remaining = {key for key in self._poll_failed_classes if key[0] != class_name}
        had_failure = remaining != self._poll_failed_classes
        self._poll_failed_classes = remaining
        if had_failure:
            self.logger.info(f"{class_name} polling recovered")

    def _mark_class_unavailable(self, registry, state_key):
        """On a REST poll failure for one class (the fetch itself failed --
        not a per-id absence from an otherwise-successful list): proactively
        push `{state_key: unavailable}` + `connected` to every registered
        device of that class, WITHOUT touching any other state. Poll-derived
        fields keep their last-written value in Indigo (this method never
        reads self.sensor_info/etc., so it cannot re-derive them from a
        stale cache and present that as fresh), and the in-memory cache is
        left alone by the caller so a live pulse mid-outage can still
        reconcile against it."""
        connected = self._is_connected()
        for protect_id in list(registry):
            for dev_id in sorted(registry.get(protect_id, ())):
                dev = indigo.devices.get(dev_id, None)
                if dev is None or not dev.enabled:
                    continue
                try:
                    dev.updateStatesOnServer([
                        {"key": state_key, "value": STATE_UNAVAILABLE},
                        {"key": "connected", "value": connected},
                    ])
                except Exception as exc:
                    self.logger.error(
                        f"{dev.name}: could not apply polled state "
                        f"({type(exc).__name__}: {exc}) - the event socket is unaffected"
                    )
                    self.logger.debug("polled-state write traceback", exc_info=True)

    def _warn_absent_from_list(self, class_name, protect_id, registry):
        """A registered id that a successful list poll no longer mentions:
        the device may have been removed from Protect, or the API simply
        returned an empty list. WARNING once per (class, id) absence-episode
        -- cleared by _clear_absent_from_list the moment it reappears."""
        key = (class_name, protect_id)
        if key in self._absent_from_list_reported:
            return
        self._absent_from_list_reported.add(key)
        for dev_id in sorted(registry.get(protect_id, ())):
            dev = indigo.devices.get(dev_id, None)
            name = dev.name if dev is not None else f"device {dev_id}"
            self.logger.warning(
                f"{name}: {class_name} {protect_id} is not in the controller's list - "
                "removed from Protect, or the API returned an empty list; keeping "
                "last-known values"
            )

    def _clear_absent_from_list(self, class_name, protect_id):
        self._absent_from_list_reported.discard((class_name, protect_id))

    def _poll_sensors(self):
        # Captured BEFORE the request goes out (see docs/CONTRACT.md) -- a
        # live pulse that arrives while this REST call is in flight must
        # never be mistaken for older than a poll that, from the pulse's
        # point of view, hasn't finished yet.
        now_ms = int(time.time() * 1000)
        try:
            sensors = self._rest(self.api.get_sensors)
        except ProtectAPIError as exc:
            self._report_poll_failure("sensor", exc)
            self._mark_class_unavailable(self.sensors, "sensorState")
            return
        except Exception as exc:
            # A non-ProtectAPIError here (a bug, or a fake in a test) must
            # be treated exactly like a ProtectAPIError -- it is still a
            # poll failure, not grounds to let the exception reach
            # _poll_devices/_pump and be mistaken for an event-socket fault.
            self._report_poll_failure("sensor", exc)
            self.logger.debug("sensor poll traceback", exc_info=True)
            self._mark_class_unavailable(self.sensors, "sensorState")
            return
        self._clear_poll_failure("sensor")
        self.sensor_info = {s["id"]: s for s in sensors if s.get("id")}
        for sensor_id in list(self.sensors):
            info = self.sensor_info.get(sensor_id)
            if info is None:
                self._warn_absent_from_list("sensor", sensor_id, self.sensors)
                # No poll_timestamp_ms -- lastPoll must not advance for a
                # device the list poll didn't actually confirm, and
                # _device_last_poll_ms is deliberately not touched either.
                self._apply_sensor_state(sensor_id, force=True)
                continue
            self._clear_absent_from_list("sensor", sensor_id)
            was_active = self.tracker.family_active(FAMILY_SENSOR_MOTION, sensor_id)
            poll_says_active = info.get("isMotionDetected")
            if poll_says_active is False and was_active:
                self.tracker.clear_family(FAMILY_SENSOR_MOTION, sensor_id)
                self.logger.debug(
                    f"sensor {sensor_id}: poll says motion has stopped - correcting the "
                    "tracker (a lost 'end' frame would look like this)"
                )
            elif poll_says_active is True and not was_active:
                self.logger.debug(
                    f"sensor {sensor_id}: poll says motion is active but the tracker is "
                    "idle - trusting the tracker (a lost 'add' frame would look like this)"
                )
            self._apply_sensor_state(sensor_id, force=True, poll_timestamp_ms=now_ms)
            # Advanced only after the write attempt, per docs/CONTRACT.md.
            self._device_last_poll_ms[sensor_id] = now_ms

    def _poll_lights(self):
        now_ms = int(time.time() * 1000)
        try:
            lights = self._rest(self.api.get_lights)
        except ProtectAPIError as exc:
            self._report_poll_failure("light", exc)
            self._mark_class_unavailable(self.lights, "lightState")
            return
        except Exception as exc:
            self._report_poll_failure("light", exc)
            self.logger.debug("light poll traceback", exc_info=True)
            self._mark_class_unavailable(self.lights, "lightState")
            return
        self._clear_poll_failure("light")
        self.light_info = {l["id"]: l for l in lights if l.get("id")}
        for light_id in list(self.lights):
            info = self.light_info.get(light_id)
            if info is None:
                self._warn_absent_from_list("light", light_id, self.lights)
                self._apply_light_state(light_id, force=True)
                continue
            self._clear_absent_from_list("light", light_id)
            self._apply_light_state(light_id, force=True, poll_timestamp_ms=now_ms)
            self._device_last_poll_ms[light_id] = now_ms

    def _poll_chimes(self):
        now_ms = int(time.time() * 1000)
        try:
            chimes = self._rest(self.api.get_chimes)
        except ProtectAPIError as exc:
            self._report_poll_failure("chime", exc)
            self._mark_class_unavailable(self.chimes, "chimeState")
            return
        except Exception as exc:
            self._report_poll_failure("chime", exc)
            self.logger.debug("chime poll traceback", exc_info=True)
            self._mark_class_unavailable(self.chimes, "chimeState")
            return
        self._clear_poll_failure("chime")
        self.chime_info = {c["id"]: c for c in chimes if c.get("id")}
        for chime_id in list(self.chimes):
            info = self.chime_info.get(chime_id)
            if info is None:
                self._warn_absent_from_list("chime", chime_id, self.chimes)
                self._apply_chime_state(chime_id, force=True)
                continue
            self._clear_absent_from_list("chime", chime_id)
            self._apply_chime_state(chime_id, force=True, poll_timestamp_ms=now_ms)
            self._device_last_poll_ms[chime_id] = now_ms

    def _poll_nvr(self):
        now_ms = int(time.time() * 1000)
        try:
            nvr = self._rest(self.api.get_nvr)
        except ProtectAPIError as exc:
            self._report_poll_failure("nvr", exc)
            self._mark_class_unavailable(self.nvrs, "armStatus")
            return
        except Exception as exc:
            self._report_poll_failure("nvr", exc)
            self.logger.debug("nvr poll traceback", exc_info=True)
            self._mark_class_unavailable(self.nvrs, "armStatus")
            return
        self._clear_poll_failure("nvr")
        self.nvr_info = nvr
        self._rekey_nvr(nvr.get("id"))
        try:
            meta = self._rest(self.api.get_meta_info)
        except Exception as exc:
            # Cosmetic only (protectVersion) -- must never block the
            # arm-state write below, which is the whole point of this
            # device. Caught broadly, not just ProtectAPIError, for the
            # same reason every other poll fetch above is.
            self.logger.debug(f"Could not refresh Protect version: {exc}")
        else:
            version = meta.get("applicationVersion") if isinstance(meta, dict) else None
            if version:
                self._protect_version = version
        self._apply_nvr_state(force=True, poll_timestamp_ms=now_ms)

    def _rekey_nvr(self, real_id):
        """There is exactly one NVR, registered under the placeholder key
        "nvr" until the first successful poll tells us its real id. Move
        the device-id set across once we know it, so later lookups key by
        the real id like every other registry does."""
        if not real_id or real_id == self._nvr_known_id:
            return
        old_key = self._nvr_known_id or "nvr"
        if old_key in self.nvrs and old_key != real_id:
            self.nvrs.setdefault(real_id, set()).update(self.nvrs.pop(old_key))
        self._nvr_known_id = real_id

    @staticmethod
    def _iso_or_empty(epoch_ms):
        if isinstance(epoch_ms, (int, float)) and epoch_ms > 0:
            return datetime.fromtimestamp(epoch_ms / 1000.0).isoformat(timespec="seconds")
        return ""

    @staticmethod
    def _newest_poll_timestamp(info, *keys):
        """Newest of one or more poll timestamp fields (e.g. a sensor's
        leakDetectedAt/externalLeakDetectedAt), or None if `info` is falsy
        or none of `keys` are present/numeric. Used for the lastLeak/
        lastAlarm/lastTamper 'last known' states -- see docs/CONTRACT.md on
        the recovery limitation these exist to at least make visible."""
        if not info:
            return None
        values = [info.get(key) for key in keys]
        real = [value for value in values if isinstance(value, (int, float))]
        return max(real) if real else None

    def _apply_polled_write(self, dev, write_fn, *args):
        """Every per-device write for the new (issue #8) device classes goes
        through here. A write failure (a bad Indigo state value, a server
        hiccup inside updateStatesOnServer, ...) must NEVER be allowed to
        propagate up into _poll_devices/_pump/_open_socket -- that would be
        mistaken for an event-socket fault and tear down the camera
        connection for a completely unrelated bug in polled-device code."""
        try:
            write_fn(dev, *args)
        except Exception as exc:
            self.logger.error(
                f"{dev.name}: could not apply polled state ({type(exc).__name__}: {exc}) - "
                "the event socket is unaffected"
            )
            self.logger.debug("polled-state write traceback", exc_info=True)

    # -- Sensor state -----------------------------------------------------

    def _apply_sensor_state(self, sensor_id, connected=None, force=False, poll_timestamp_ms=None):
        if connected is None:
            connected = self._is_connected()
        for dev_id in sorted(self.sensors.get(sensor_id, ())):
            dev = indigo.devices.get(dev_id, None)
            if dev is None or not dev.enabled:
                continue
            self._apply_polled_write(
                dev, self._write_sensor_states, sensor_id, connected, force, poll_timestamp_ms)

    def _resolve_sensor_primary_state(self, dev, mount_type):
        choice = dev.pluginProps.get("primaryState", "auto")
        if choice and choice != "auto":
            return choice
        return SENSOR_MOUNT_PRIMARY_STATE.get(mount_type, "motion")

    def _sensor_open_state(self, sensor_id, info):
        """isOpen = poll's isOpened, overridden by whichever of sensorOpened/
        sensorClosed has the newer `start` versus the poll's own
        openStatusChangedAt -- or, when that field is null (both entries in
        tests/fixtures/sensors_spec.json have it null; it may simply be
        common), versus this device's last-poll baseline
        (_device_last_poll_ms) instead. Without that fallback a single
        stale pulse wins forever whenever openStatusChangedAt is null,
        since `None` compares as "always older" against a real timestamp --
        the fallback makes the override self-expire at the next poll like
        every other pulse override in this module.

        That poll-baseline fallback is a COMPARISON ANCHOR ONLY -- it is
        our own poll cadence, not a fact about the device, and must never
        be reported as `lastOpenChange` itself: doing so made
        `lastOpenChange` advance by one poll interval every cycle forever
        whenever `openStatusChangedAt` was null, even though nothing about
        the sensor had changed. Returns (is_open, reportable_ms), where
        `reportable_ms` is `None` unless the winning timestamp came from a
        REAL source -- `openStatusChangedAt` itself, or an actual
        `sensorOpened`/`sensorClosed` pulse -- so callers can leave
        `lastOpenChange` untouched (never fabricate a "changed at" out of
        nothing but our own polling clock).
        """
        is_open = bool(info.get("isOpened")) if info else False
        changed_at = info.get("openStatusChangedAt") if info else None
        if isinstance(changed_at, (int, float)):
            winning_ms = changed_at
            reportable_ms = changed_at
        else:
            winning_ms = self._device_last_poll_ms.get(sensor_id)
            reportable_ms = None

        for pulse_type, value in (("sensorOpened", True), ("sensorClosed", False)):
            pulse = self.tracker.last_pulse(sensor_id, pulse_type)
            start = pulse.get("start") if pulse else None
            if not isinstance(start, (int, float)):
                continue
            if winning_ms is None or start > winning_ms:
                winning_ms = start
                reportable_ms = start
                is_open = value
            else:
                self.logger.debug(
                    f"sensor {sensor_id}: discarding {pulse_type} pulse (start={start}) - "
                    f"older than the poll baseline ({winning_ms})"
                )
        return is_open, reportable_ms

    def _sensor_battery_low(self, sensor_id, info):
        """batteryLow = poll batteryStatus.isLow OR a sensorBatteryLow pulse
        newer than this device's last poll write."""
        battery_low = bool(_as_dict(info.get("batteryStatus")).get("isLow")) if info else False
        pulse = self.tracker.last_pulse(sensor_id, "sensorBatteryLow")
        start = pulse.get("start") if pulse else None
        last_poll_ms = self._device_last_poll_ms.get(sensor_id)
        if isinstance(start, (int, float)):
            if last_poll_ms is None or start > last_poll_ms:
                battery_low = True
            else:
                self.logger.debug(
                    f"sensor {sensor_id}: discarding sensorBatteryLow pulse (start={start}) - "
                    f"older than the poll baseline ({last_poll_ms})"
                )
        return battery_low

    def _sensor_metric(self, sensor_id, info, metric_key):
        """temperature/humidity/lightLevel: poll's stats.<metric_key>.value,
        immediately overridden by a sensorExtremeValues pulse for the same
        metric newer than this device's last poll write -- self-expiring,
        the next poll always re-baselines. `metric_key` is one of
        "temperature"/"humidity"/"light" (matching both the `stats` object's
        keys and the pulse's metadata.sensorType.text values)."""
        value = None
        if info:
            raw = _as_dict(_as_dict(info.get("stats")).get(metric_key)).get("value")
            if _is_real_number(raw):
                value = float(raw)

        pulse = self.tracker.last_pulse(sensor_id, "sensorExtremeValues")
        if pulse:
            start = pulse.get("start")
            last_poll_ms = self._device_last_poll_ms.get(sensor_id)
            if isinstance(start, (int, float)):
                if last_poll_ms is None or start > last_poll_ms:
                    metadata = _as_dict(pulse.get("metadata"))
                    sensor_type = _as_dict(metadata.get("sensorType")).get("text")
                    if sensor_type == metric_key:
                        raw = _as_dict(metadata.get("sensorValue")).get("text")
                        if _is_real_number(raw):
                            value = float(raw)
                else:
                    self.logger.debug(
                        f"sensor {sensor_id}: discarding sensorExtremeValues pulse "
                        f"(start={start}) - older than the poll baseline ({last_poll_ms})"
                    )
        return value

    def _sensor_last_motion_ms(self, sensor_id, info):
        """Merges the tracker's own last_family_ms(FAMILY_SENSOR_MOTION,
        ...) with the poll's motionDetectedAt (newest wins) -- mirrors
        _light_last_motion_ms's poll+pulse merge. Without this, lastMotion
        reads "" after every plugin restart even though the poll object
        carries a real motionDetectedAt timestamp, because the tracker
        (pure in-memory state) has no memory of events from before this
        run started."""
        tracker_ms = self.tracker.last_family_ms(FAMILY_SENSOR_MOTION, sensor_id)
        poll_ms = info.get("motionDetectedAt") if info else None
        if isinstance(poll_ms, (int, float)) and (
                not isinstance(tracker_ms, (int, float)) or poll_ms > tracker_ms):
            return poll_ms
        return tracker_ms if isinstance(tracker_ms, (int, float)) else None

    def _write_sensor_states(self, dev, sensor_id, connected, force, poll_timestamp_ms):
        info = self.sensor_info.get(sensor_id)

        # Lifecycle booleans: live-event-driven, so honesty rule applies --
        # False whenever the socket is down, exactly like camera motion.
        # Knowable and safe to write even with no poll cache at all -- the
        # tracker's answer ("no event has happened") is genuine information,
        # not a fabrication, unlike a poll-derived field we've simply never
        # read.
        motion_active = self.tracker.family_active(FAMILY_SENSOR_MOTION, sensor_id) if connected else False
        leak_active = self.tracker.family_active(FAMILY_SENSOR_LEAK, sensor_id) if connected else False
        alarm_active = self.tracker.family_active(FAMILY_SENSOR_ALARM, sensor_id) if connected else False
        alarm_types = self.tracker.family_types(FAMILY_SENSOR_ALARM, sensor_id) if connected else set()
        tamper_active = self.tracker.family_active(FAMILY_SENSOR_TAMPER, sensor_id) if connected else False
        last_motion_ms = self._sensor_last_motion_ms(sensor_id, info)

        states = [
            {"key": "motionDetected", "value": motion_active},
            {"key": "leakDetected", "value": leak_active},
            {"key": "alarmTriggered", "value": alarm_active},
            {"key": "alarmType", "value": ",".join(sorted(alarm_types))},
            {"key": "tampered", "value": tamper_active},
            {"key": "sensorState", "value": _state_or_unavailable(info)},
            {"key": "connected", "value": connected},
            {"key": "lastMotion", "value": self._iso_or_empty(last_motion_ms)},
        ]

        # Poll-derived (with live pulse override): omitted entirely -- not
        # written as a fabricated False/"" -- when there is no poll cache
        # yet (brand-new device before the first poll, a REST failure that
        # never populated the cache, or an id absent from a successful
        # list). Kept at last-known when info IS present but the socket is
        # down, exactly like camera hardware/config states.
        mount_type = None
        if info:
            is_open, open_change_ms = self._sensor_open_state(sensor_id, info)
            battery_low = self._sensor_battery_low(sensor_id, info)
            temperature = self._sensor_metric(sensor_id, info, "temperature")
            humidity = self._sensor_metric(sensor_id, info, "humidity")
            light_level = self._sensor_metric(sensor_id, info, "light")
            mount_type = info.get("mountType") or ""

            states.append({"key": "isOpen", "value": is_open})
            states.append({"key": "batteryLow", "value": battery_low})
            states.append({"key": "mountType", "value": mount_type})
            # open_change_ms is None unless the winning timestamp was REAL
            # (openStatusChangedAt itself, or an actual pulse) -- never the
            # internal poll-baseline fallback. Omitted, not written as "",
            # when there is nothing real to report, so the state is simply
            # never re-written every poll interval for no reason.
            if open_change_ms is not None:
                states.append({"key": "lastOpenChange", "value": self._iso_or_empty(open_change_ms)})
            if temperature is not None:
                states.append({"key": "temperature", "value": temperature})
            if humidity is not None:
                states.append({"key": "humidity", "value": humidity})
            if light_level is not None:
                states.append({"key": "lightLevel", "value": light_level})

            # Recovery limitation (see docs/CONTRACT.md and the README
            # banner): the poll object has no "currently active" flag for
            # leak/alarm/tamper, only a *DetectedAt timestamp -- so after a
            # plugin restart, leakDetected/alarmTriggered/tampered read
            # False until a NEW live event arrives, even if the sensor's
            # last-reported condition was still active. Rather than
            # fabricate a currently-active guess from a stale timestamp,
            # these three timestamps are surfaced as their own states so
            # the information is at least visible. Skipped (not written as
            # "") when the poll never reported one.
            last_leak_ms = self._newest_poll_timestamp(
                info, "leakDetectedAt", "externalLeakDetectedAt")
            if last_leak_ms is not None:
                states.append({"key": "lastLeak", "value": self._iso_or_empty(last_leak_ms)})
            last_alarm_ms = self._newest_poll_timestamp(info, "alarmTriggeredAt")
            if last_alarm_ms is not None:
                states.append({"key": "lastAlarm", "value": self._iso_or_empty(last_alarm_ms)})
            last_tamper_ms = self._newest_poll_timestamp(info, "tamperingDetectedAt")
            if last_tamper_ms is not None:
                states.append({"key": "lastTamper", "value": self._iso_or_empty(last_tamper_ms)})

            # The OpenAPI spec marks batteryStatus "[DEPRECATED] Use
            # wirelessConnectionState.batteryStatus instead" -- but
            # `wirelessConnectionState` does not appear anywhere else in
            # the spec (no schema defines it). Real firmware may still
            # populate this "deprecated" field, or may have already moved
            # to the undocumented one; this is a likely first hardware-
            # report item once a real Protect sensor is available.
            battery_pct = _as_dict(info.get("batteryStatus")).get("percentage")
            if _is_real_number(battery_pct):
                # batteryLevel is Indigo's NATIVE property (SupportsBatteryLevel
                # in Devices.xml) -- deliberately NOT in the `states` batch
                # above; writing it needs its own updateStateOnServer call.
                dev.updateStateOnServer("batteryLevel", value=int(battery_pct))
        else:
            is_open = None   # unknown -- only a lifecycle-derived primary can drive onOffState

        # onOffState: "open" is the one primary choice backed by a
        # poll-derived value, so it is only written once we actually have
        # one -- never a fabricated False before the first poll. "motion"/
        # "leak"/"alarm" are lifecycle booleans and are always knowable.
        primary = self._resolve_sensor_primary_state(dev, mount_type or "")
        lifecycle_by_primary = {"motion": motion_active, "leak": leak_active, "alarm": alarm_active}
        if primary == "open":
            on_state = is_open
        else:
            on_state = lifecycle_by_primary[primary]
        if on_state is not None:
            states.append({"key": "onOffState", "value": on_state})

        if poll_timestamp_ms is not None:
            states.append({"key": "lastPoll", "value": self._iso_or_empty(poll_timestamp_ms)})

        if on_state is not None and (force or on_state != bool(dev.states.get("onOffState", False))):
            if primary == "motion":
                image = (indigo.kStateImageSel.MotionSensorTripped if on_state
                         else indigo.kStateImageSel.MotionSensor)
            else:
                image = indigo.kStateImageSel.SensorOn if on_state else indigo.kStateImageSel.SensorOff
            dev.updateStateImageOnServer(image)
        dev.updateStatesOnServer(states)

    # -- Light state --------------------------------------------------------

    def _apply_light_state(self, light_id, connected=None, force=False, poll_timestamp_ms=None):
        if connected is None:
            connected = self._is_connected()
        for dev_id in sorted(self.lights.get(light_id, ())):
            dev = indigo.devices.get(dev_id, None)
            if dev is None or not dev.enabled:
                continue
            self._apply_polled_write(
                dev, self._write_light_states, light_id, connected, force, poll_timestamp_ms)

    def _light_last_motion_ms(self, light_id, info):
        poll_last_motion = info.get("lastMotion") if info else None
        pulse = self.tracker.last_pulse(light_id, "lightMotion")
        pulse_start = pulse.get("start") if pulse else None
        if isinstance(pulse_start, (int, float)) and (
                not isinstance(poll_last_motion, (int, float)) or pulse_start > poll_last_motion):
            return pulse_start
        return poll_last_motion if isinstance(poll_last_motion, (int, float)) else None

    def _write_light_states(self, dev, light_id, connected, force, poll_timestamp_ms):
        info = self.light_info.get(light_id)

        states = [
            {"key": "lightState", "value": _state_or_unavailable(info)},
            {"key": "connected", "value": connected},
        ]

        # pirMotionDetected is the one live-event-influenced field on a
        # light (via the lightMotion pulse) -- treated like a sensor's
        # lifecycle booleans: forced False when the socket is down, per
        # docs/CONTRACT.md pairing it with isMotionDetected under the same
        # disconnect rule. Always knowable (False is genuine information
        # when the socket is up and nothing has fired), so always written.
        pir_motion = False
        if connected:
            pulse = self.tracker.last_pulse(light_id, "lightMotion")
            start = pulse.get("start") if pulse else None
            last_poll_ms = self._device_last_poll_ms.get(light_id)
            if isinstance(start, (int, float)) and (last_poll_ms is None or start > last_poll_ms):
                pir_motion = True
            elif isinstance(start, (int, float)):
                self.logger.debug(
                    f"light {light_id}: discarding lightMotion pulse (start={start}) - "
                    f"older than the poll baseline ({last_poll_ms})"
                )
                pir_motion = bool(info.get("isPirMotionDetected")) if info else False
            elif info:
                pir_motion = bool(info.get("isPirMotionDetected"))
        states.append({"key": "pirMotionDetected", "value": pir_motion})
        states.append({"key": "lastMotion",
                        "value": self._iso_or_empty(self._light_last_motion_ms(light_id, info))})

        # Poll-derived: omitted (not fabricated) when there is no cache yet.
        # onOffState is the light's actual LED (isLightOn), with no
        # lifecycle-boolean fallback at all -- writing False here before the
        # first poll is exactly the "every restart fires 'Floodlight turned
        # off'" bug this gate exists to prevent.
        if info:
            is_light_on = bool(info.get("isLightOn"))
            states.append({"key": "onOffState", "value": is_light_on})
            states.append({"key": "isDark", "value": bool(info.get("isDark"))})
            states.append({"key": "forceEnabled", "value": bool(info.get("isLightForceEnabled"))})
            states.append({"key": "lightMode",
                            "value": _as_dict(info.get("lightModeSettings")).get("mode") or ""})
            try:
                led_level = _as_dict(info.get("lightDeviceSettings"))["ledLevel"]
                if isinstance(led_level, bool):
                    # int(True) == 1 -- a real-looking but fabricated LED level.
                    raise TypeError("ledLevel must not be a bool")
                states.append({"key": "ledLevel", "value": int(led_level)})
            except (KeyError, TypeError, ValueError):
                # Missing, a bool, or otherwise unparseable -- skipped, not
                # defaulted to 0, same rule as camera micVolume.
                pass

        if poll_timestamp_ms is not None:
            states.append({"key": "lastPoll", "value": self._iso_or_empty(poll_timestamp_ms)})

        dev.updateStatesOnServer(states)

    def actionControlDevice(self, action, dev):
        """Only protectLight devices reach here -- relay/dimmer/sensor
        universal actions (TurnOn/TurnOff/Toggle) route through this
        callback, but protectCamera/protectSensor declare no such
        capability in Devices.xml, so Indigo never calls it for them."""
        if dev.deviceTypeId != "protectLight":
            return
        light_id = dev.pluginProps.get("lightId", "")
        if not light_id:
            self.logger.error(f"{dev.name}: no light selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured; cannot control the light.")
            return

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            force_enabled = True
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            force_enabled = False
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            # Never guess before a poll: if nothing is cached yet (e.g.
            # right after startup), read the light first rather than
            # assuming isLightForceEnabled is False.
            cached = self.light_info.get(light_id)
            if cached is None:
                try:
                    cached = self._rest(self.api.get_light, light_id)
                except ProtectAPIError as exc:
                    self.logger.error(
                        f"{dev.name}: could not read the light to toggle it "
                        f"({self._describe_api_error(exc, entity='light')})"
                    )
                    return
                self.light_info[light_id] = cached
            force_enabled = not bool((cached or {}).get("isLightForceEnabled"))
        else:
            return

        try:
            patch_response = self._rest(self.api.patch_light, light_id,
                                         {"isLightForceEnabled": force_enabled})
        except ProtectAPIError as exc:
            self.logger.error(
                f"{dev.name}: could not set light ({self._describe_api_error(exc, entity='light')})"
            )
            return

        # The PATCH itself succeeded -- merge its response (which may be
        # partial) into the cache immediately, so even if the re-GET below
        # fails we don't lose the fact that the force flag actually changed.
        cached = self.light_info.get(light_id) or {}
        if isinstance(patch_response, dict) and patch_response:
            self.light_info[light_id] = {**cached, **patch_response}
        else:
            self.light_info[light_id] = {**cached, "isLightForceEnabled": force_enabled}

        try:
            info = self._rest(self.api.get_light, light_id)
        except ProtectAPIError as exc:
            self.logger.error(
                f"{dev.name}: force flag set, but could not re-read the light "
                f"({self._describe_api_error(exc, entity='light')}) - "
                "states may be stale until the next poll"
            )
            info = None

        if info:
            self.light_info[light_id] = info

        now_ms = int(time.time() * 1000)
        self._apply_light_state(light_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[light_id] = now_ms
        if force_enabled:
            self.logger.info(f"{dev.name}: force-enabled")
        else:
            self.logger.info(
                f"{dev.name}: force-enable cleared - the floodlight may still turn on "
                "by its own motion mode"
            )

    def setLightLevel(self, action, dev):
        light_id = dev.pluginProps.get("lightId", "")
        if not light_id:
            self.logger.error(f"{dev.name}: no light selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        raw = action.props.get("ledLevel", "")
        try:
            level = int(raw)
        except (TypeError, ValueError):
            self.logger.error(f"{dev.name}: LED level {raw!r} is not a number (must be 1-6).")
            return
        if not 1 <= level <= 6:
            self.logger.error(f"{dev.name}: LED level {level} is out of range (must be 1-6).")
            return

        try:
            response = self._rest(self.api.patch_light, light_id,
                                   {"lightDeviceSettings": {"ledLevel": level}})
        except ProtectAPIError as exc:
            self.logger.error(
                f"{dev.name}: could not set LED level ({self._describe_api_error(exc, entity='light')})"
            )
            return

        cached = self.light_info.get(light_id) or {}
        if isinstance(response, dict) and response:
            # Merge, never replace -- a PATCH response can be a partial
            # object, and replacing the cache with it would silently drop
            # every other previously-known field.
            self.light_info[light_id] = {**cached, **response}
        else:
            # A None/empty PATCH response is still a success (some PATCH
            # endpoints return nothing useful) -- re-GET for an
            # authoritative view rather than guessing the new shape.
            try:
                info = self._rest(self.api.get_light, light_id)
            except ProtectAPIError as exc:
                self.logger.error(
                    f"{dev.name}: LED level set, but could not re-read the light "
                    f"({self._describe_api_error(exc, entity='light')}) - "
                    "states may be stale until the next poll"
                )
                info = None
            if info:
                self.light_info[light_id] = info

        now_ms = int(time.time() * 1000)
        self._apply_light_state(light_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[light_id] = now_ms
        self.logger.info(f"{dev.name}: LED level set to {level}")

    # -- Chime state --------------------------------------------------------

    def _apply_chime_state(self, chime_id, connected=None, force=False, poll_timestamp_ms=None):
        if connected is None:
            connected = self._is_connected()
        for dev_id in sorted(self.chimes.get(chime_id, ())):
            dev = indigo.devices.get(dev_id, None)
            if dev is None or not dev.enabled:
                continue
            self._apply_polled_write(
                dev, self._write_chime_states, chime_id, connected, force, poll_timestamp_ms)

    def _write_chime_states(self, dev, chime_id, connected, force, poll_timestamp_ms):
        info = self.chime_info.get(chime_id)
        states = [
            {"key": "chimeState", "value": _state_or_unavailable(info)},
            {"key": "connected", "value": connected},
        ]
        if info:
            states.append({"key": "pairedCameraCount", "value": len(info.get("cameraIds") or [])})
            ring_settings = info.get("ringSettings") or []
            if ring_settings and isinstance(ring_settings[0], dict):
                try:
                    volume = ring_settings[0]["volume"]
                    if isinstance(volume, bool):
                        # int(True) == 1 -- a real-looking but fabricated volume.
                        raise TypeError("volume must not be a bool")
                    states.append({"key": "ringVolume", "value": int(volume)})
                except (KeyError, TypeError, ValueError):
                    pass
        if poll_timestamp_ms is not None:
            states.append({"key": "lastPoll", "value": self._iso_or_empty(poll_timestamp_ms)})
        dev.updateStatesOnServer(states)

    def setChimeVolume(self, action, dev):
        chime_id = dev.pluginProps.get("chimeId", "")
        if not chime_id:
            self.logger.error(f"{dev.name}: no chime selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        raw = action.props.get("volume", "")
        try:
            volume = int(raw)
        except (TypeError, ValueError):
            self.logger.error(f"{dev.name}: volume {raw!r} is not a number (must be 0-100).")
            return
        if not 0 <= volume <= 100:
            self.logger.error(f"{dev.name}: volume {volume} is out of range (must be 0-100).")
            return

        # Never guess before a poll: if nothing is cached yet (e.g. right
        # after startup or a poll failure), read the chime first rather
        # than reporting "no ringSettings" without having actually looked
        # -- mirrors the light Toggle path's same rule.
        cached = self.chime_info.get(chime_id)
        if cached is None:
            try:
                cached = self._rest(self.api.get_chime, chime_id)
            except ProtectAPIError as exc:
                self.logger.error(
                    f"{dev.name}: could not read the chime to set its volume "
                    f"({self._describe_api_error(exc, entity='chime')})"
                )
                return
            self.chime_info[chime_id] = cached

        ring_settings = (cached or {}).get("ringSettings") or []
        if not ring_settings:
            self.logger.error(
                f"{dev.name}: this chime has no ringSettings (no paired doorbell cameras) - "
                "cannot set volume."
            )
            return
        # The PATCH item schema is additionalProperties: false, unlike the
        # GET schema -- echoing a cached entry wholesale (which could carry
        # extra fields, e.g. a future API version) can be rejected by the
        # controller. Send only the four documented ringSettings keys.
        new_settings = [
            {"cameraId": entry.get("cameraId"), "repeatTimes": entry.get("repeatTimes"),
             "ringtoneId": entry.get("ringtoneId"), "volume": volume}
            for entry in ring_settings if isinstance(entry, dict)
        ]

        try:
            response = self._rest(self.api.patch_chime, chime_id, {"ringSettings": new_settings})
        except ProtectAPIError as exc:
            self.logger.error(
                f"{dev.name}: could not set chime volume ({self._describe_api_error(exc, entity='chime')})"
            )
            return

        if isinstance(response, dict) and response:
            self.chime_info[chime_id] = {**cached, **response}
        else:
            try:
                info = self._rest(self.api.get_chime, chime_id)
            except ProtectAPIError as exc:
                self.logger.error(
                    f"{dev.name}: chime volume set, but could not re-read the chime "
                    f"({self._describe_api_error(exc, entity='chime')}) - "
                    "states may be stale until the next poll"
                )
                info = None
            if info:
                self.chime_info[chime_id] = info

        now_ms = int(time.time() * 1000)
        self._apply_chime_state(chime_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[chime_id] = now_ms
        self.logger.info(f"{dev.name}: ring volume set to {volume}")

    # -- NVR state ------------------------------------------------------

    def _apply_nvr_state(self, connected=None, force=False, poll_timestamp_ms=None):
        if connected is None:
            connected = self._is_connected()
        key = self._nvr_known_id or "nvr"
        for dev_id in sorted(self.nvrs.get(key, ())):
            dev = indigo.devices.get(dev_id, None)
            if dev is None or not dev.enabled:
                continue
            self._apply_polled_write(dev, self._write_nvr_states, connected, force, poll_timestamp_ms)

    def _write_nvr_states(self, dev, connected, force, poll_timestamp_ms):
        info = self.nvr_info
        arm_mode = _as_dict(info.get("armMode")) if info else {}
        arm_status = (arm_mode.get("status") or STATE_UNAVAILABLE) if info else STATE_UNAVAILABLE

        states = [
            {"key": "armStatus", "value": arm_status},
            {"key": "connected", "value": connected},
        ]
        if info:
            states.append({"key": "nvrName", "value": info.get("name") or ""})
            model = info.get("type")
            if model:
                states.append({"key": "nvrModel", "value": str(model)})
            # protectVersion is written only once /meta/info has EVER
            # succeeded (self._protect_version starts "" and is only ever
            # updated on a successful get_meta_info -- see _poll_nvr) --
            # never a fabricated value from a poll that only refreshed the
            # arm state.
            if self._protect_version:
                states.append({"key": "protectVersion", "value": self._protect_version})
            states.append({"key": "armedAt", "value": self._iso_or_empty(arm_mode.get("armedAt"))})
            states.append({"key": "breachDetectedAt",
                            "value": self._iso_or_empty(arm_mode.get("breachDetectedAt"))})
            try:
                breach_count = arm_mode.get("breachEventCount", 0)
                if isinstance(breach_count, bool):
                    # int(True) == 1 -- a real-looking but fabricated count.
                    raise TypeError("breachEventCount must not be a bool")
                states.append({"key": "breachEventCount", "value": int(breach_count)})
            except (TypeError, ValueError):
                pass
        if poll_timestamp_ms is not None:
            states.append({"key": "lastPoll", "value": self._iso_or_empty(poll_timestamp_ms)})
        dev.updateStatesOnServer(states)

    # ------------------------------------------------------------------
    # Actions and menu items
    # ------------------------------------------------------------------
    def actionControlUniversal(self, action, dev):
        """Devices.xml sets SupportsStatusRequest, so Indigo shows "Send Status
        Request" on every device type. Declaring the capability and then doing
        nothing is worse than not declaring it -- this is the first thing a user
        tries on a sensor that looks stuck."""
        if action.deviceAction != indigo.kUniversalAction.RequestStatus:
            return
        device_type = dev.deviceTypeId
        if device_type == "protectSensor":
            self._request_status_sensor(dev)
        elif device_type == "protectLight":
            self._request_status_light(dev)
        elif device_type == "protectChime":
            self._request_status_chime(dev)
        elif device_type == "protectNvr":
            self._request_status_nvr(dev)
        else:
            self._request_status_camera(dev)

    def _request_status_camera(self, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: no camera selected.")
            return
        self._refresh_camera_info()
        self._apply_camera_state(camera_id, force=True)
        self._refresh_stream_urls_sync(dev)
        self.logger.info(
            f"{dev.name}: refreshed (event socket "
            f"{'connected' if self._is_connected() else 'DOWN'})"
        )

    def _request_status_sensor(self, dev):
        sensor_id = dev.pluginProps.get("sensorId", "")
        if not sensor_id:
            self.logger.error(f"{dev.name}: no sensor selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            info = self._rest(self.api.get_sensor, sensor_id)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: could not refresh ({self._describe_api_error(exc, entity='sensor')})")
            return
        # A successful single-device read is as good evidence of recovery
        # as a successful list poll -- clear the same guard so a user who
        # fixes the controller and immediately hits "Send Status Request"
        # isn't left staring at a stale ERROR from the last list-poll outage.
        self._clear_poll_failure("sensor")
        self.sensor_info[sensor_id] = info
        was_active = self.tracker.family_active(FAMILY_SENSOR_MOTION, sensor_id)
        if info.get("isMotionDetected") is False and was_active:
            self.tracker.clear_family(FAMILY_SENSOR_MOTION, sensor_id)
        now_ms = int(time.time() * 1000)
        self._apply_sensor_state(sensor_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[sensor_id] = now_ms
        self.logger.info(f"{dev.name}: refreshed")

    def _request_status_light(self, dev):
        light_id = dev.pluginProps.get("lightId", "")
        if not light_id:
            self.logger.error(f"{dev.name}: no light selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            info = self._rest(self.api.get_light, light_id)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: could not refresh ({self._describe_api_error(exc, entity='light')})")
            return
        self._clear_poll_failure("light")
        self.light_info[light_id] = info
        now_ms = int(time.time() * 1000)
        self._apply_light_state(light_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[light_id] = now_ms
        self.logger.info(f"{dev.name}: refreshed")

    def _request_status_chime(self, dev):
        chime_id = dev.pluginProps.get("chimeId", "")
        if not chime_id:
            self.logger.error(f"{dev.name}: no chime selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            info = self._rest(self.api.get_chime, chime_id)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: could not refresh ({self._describe_api_error(exc, entity='chime')})")
            return
        self._clear_poll_failure("chime")
        self.chime_info[chime_id] = info
        now_ms = int(time.time() * 1000)
        self._apply_chime_state(chime_id, force=True, poll_timestamp_ms=now_ms)
        self._device_last_poll_ms[chime_id] = now_ms
        self.logger.info(f"{dev.name}: refreshed")

    def _request_status_nvr(self, dev):
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            info = self._rest(self.api.get_nvr)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: could not refresh ({self._describe_api_error(exc, entity='nvr')})")
            return
        self._clear_poll_failure("nvr")
        self.nvr_info = info
        self._rekey_nvr(info.get("id"))
        now_ms = int(time.time() * 1000)
        self._apply_nvr_state(force=True, poll_timestamp_ms=now_ms)
        self.logger.info(f"{dev.name}: refreshed")

    def takeSnapshot(self, action, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: no camera selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured; cannot take a snapshot.")
            return

        info = self.camera_info.get(camera_id, {})
        supports_hq = info.get("featureFlags", {}).get("supportFullHdSnapshot")
        try:
            data = self._rest(self.api.get_snapshot, camera_id,
                              supports_high_quality=supports_hq)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: snapshot failed - {exc}")
            # Leave the stale file in place but stop claiming it is current: a
            # control page serving an hour-old image as live is a meaningful
            # misrepresentation for a security camera.
            dev.updateStateOnServer("snapshotPath", value="")
            return

        snapshot_dir = self._get_snapshot_dir()
        os.makedirs(snapshot_dir, exist_ok=True)
        path = os.path.join(snapshot_dir, f"camera_{dev.id}.jpg")
        tmp = f"{path}.tmp"
        # Write via a temp file so a control page never serves a half-written JPEG.
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

        dev.updateStateOnServer("snapshotPath", value=path)
        self.logger.info(
            f"{dev.name}: snapshot saved ({len(data)} bytes) - control pages can "
            f"use /images/{SNAPSHOT_SUBDIR}/camera_{dev.id}.jpg"
        )

    def _get_snapshot_dir(self):
        """Where snapshots live. Resolved on first use, never in __init__.

        Deliberately NOT inside the plugin bundle: Indigo replaces Contents/ on
        every plugin upgrade, which would silently delete every snapshot. Indigo
        serves "Web Assets" over its web server, so a file written here is also
        reachable from a control page as /images/<subdir>/<name>.
        """
        if self._snapshot_dir is None:
            self._snapshot_dir = os.path.join(
                indigo.server.getInstallFolderPath(), "Web Assets", "images", SNAPSHOT_SUBDIR
            )
        return self._snapshot_dir

    @staticmethod
    def _web_page_paths(install):
        source = os.path.join(
            install, "Plugins", WEB_PAGE_BUNDLE_DIR, "Contents",
            "Resources", "pages", WEB_PAGE_FILENAME,
        )
        dest_dir = os.path.join(install, "Web Assets", "static", "pages")
        return source, dest_dir, os.path.join(dest_dir, WEB_PAGE_FILENAME)

    def _warn_if_managed_page_is_stale(self):
        """Issue #27 (X3): pref is OFF, so no write happens -- but a stale
        installed page is worth one INFO. Read-only and best-effort: any
        failure here (missing bundle, permissions, whatever) is DEBUG only,
        because an opted-out user must not get WARNINGs about a file the
        plugin isn't managing.
        """
        try:
            install = indigo.server.getInstallFolderPath()
            source, _dest_dir, dest = self._web_page_paths(install)
            if not (os.path.isfile(source) and os.path.isfile(dest)):
                return
            with open(source, "rb") as handle:
                source_bytes = handle.read()
            with open(dest, "rb") as handle:
                dest_bytes = handle.read()
            if source_bytes != dest_bytes:
                self.logger.info(
                    f"Cameras page management is off, and the installed page "
                    f"({dest}) differs from the bundled one (v{self.pluginVersion}) - "
                    "update it by hand, or re-tick 'Manage the Cameras web page' "
                    "to have the plugin do it."
                )
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.debug(f"Could not check the installed Cameras page for staleness: {exc}")

    def _sync_web_page(self, prefs=None):
        """Issue #27: install/update the bundled Cameras page into Web
        Assets on startup and on every prefs save, so users stop manually
        copying it after every change.

        Must NEVER raise out of startup: the whole body is one try/except,
        and every failure path -- missing bundle, empty bundle, unreadable/
        unwritable destination -- is a WARNING naming the destination path
        and the retry path, so the user can copy the file by hand instead.
        When the pref is off, no write happens, but `_warn_if_managed_page_
        is_stale` still flags a stale installed copy at INFO. Paths are
        resolved lazily here, never in __init__ or at module scope: Indigo
        exec()s plugin.py as a string, so __file__ does not exist (see
        _get_snapshot_dir).
        """
        prefs = self.pluginPrefs if prefs is None else prefs
        if not _truthy(prefs.get("managePage")):
            self._warn_if_managed_page_is_stale()
            return

        dest = None
        tmp = None
        try:
            install = indigo.server.getInstallFolderPath()
            source, dest_dir, dest = self._web_page_paths(install)

            if not os.path.isfile(source):
                self.logger.warning(
                    f"Cameras page not found in the plugin bundle ({source}) - "
                    f"cannot install/update it at {dest}. Reinstalling the "
                    "plugin should restore the bundled copy."
                )
                return

            with open(source, "rb") as handle:
                source_bytes = handle.read()

            if not source_bytes:
                self.logger.warning(
                    f"Bundled Cameras page at {source} is empty (truncated or "
                    f"corrupt) - refusing to install it over {dest}. "
                    "Reinstalling the plugin should restore the bundled copy."
                )
                return

            dest_bytes = None
            if os.path.isfile(dest):
                with open(dest, "rb") as handle:
                    dest_bytes = handle.read()

            if dest_bytes == source_bytes:
                self.logger.debug(f"Cameras page already up to date in Web Assets ({dest})")
                return

            os.makedirs(dest_dir, exist_ok=True)
            tmp = f"{dest}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(source_bytes)
            os.replace(tmp, dest)
            tmp = None  # installed -- nothing left to clean up

            self.logger.info(
                f"Installed/updated the Cameras page in Web Assets (v{self.pluginVersion} "
                "-> managed by the plugin; untick 'Manage the Cameras web page' to hand-edit it)"
            )
        except Exception as exc:  # pylint: disable=broad-except
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            where = dest or f"Web Assets/static/pages/{WEB_PAGE_FILENAME}"
            self.logger.warning(
                f"Could not install/update the Cameras page at {where}: {exc}. Copy "
                "the bundled copy (Contents/Resources/pages/cameras.html inside "
                "the plugin bundle) there by hand, or untick 'Manage the Cameras "
                "web page' to stop the plugin trying. The plugin will retry at "
                "the next config save or plugin restart."
            )

    # ------------------------------------------------------------------
    # Camera control actions (issue #6) -- the plugin's first write path.
    #
    # Every action shares one shape: resolve the camera and its cached
    # object via _resolve_camera (which also refreshes once if it isn't
    # cached yet), gate on the matching featureFlags entry where one
    # exists, build a partial PATCH body, and hand it to _patch_camera.
    # A refused PATCH is an ERROR in the Event Log naming what was
    # attempted and what the controller said was wrong -- camera_info is
    # only ever replaced by a 2xx response.
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_api_error(exc, entity="camera"):
        """One line of actionable text for a ProtectAPIError, keyed on
        ``exc.kind`` -- shared by every camera control action's error log
        plus ``_refresh_camera_info``, so what a given failure MEANS is
        answered identically everywhere instead of every call site
        formatting its own ``str(exc)`` HTTP-status-and-body dump.
        Deliberately NOT used by ``takeSnapshot``/``discoverCameras``,
        whose existing wording predates this and is untouched.

        ``entity`` (default ``"camera"``, so every existing camera-only
        caller is unaffected) names the noun in the ``not_found``/``shape``
        wording below -- issue #8's sensor/light/chime/NVR poll,
        RequestStatus, and action paths pass their own class name
        ("sensor"/"light"/"chime"/"nvr") so a sensor's 404 doesn't get
        told to "reselect the camera".

        Definite outcomes (auth, not_found, bad_request) name the fix.
        Uncertain outcomes (transport, server, shape) say so explicitly
        rather than implying a write failed when it may well have landed --
        ``_patch_camera`` additionally re-refreshes camera_info on those
        three so state catches up to whatever actually happened, instead of
        waiting for the plugin's next scheduled refresh.
        """
        if exc.kind == "auth":
            text = ("UniFi Protect rejected the API key. Regenerate it in UniFi OS "
                    "(Settings > Control Plane > Integrations) and update the plugin config")
            if exc.status == 403:
                text += " or the key lacks permission for this operation"
            return text + "."
        if exc.kind == "rate_limited":
            text = "rate limited by the controller"
            if exc.retry_after is not None:
                text += f" - retry in {exc.retry_after:.0f}s"
            return text
        if exc.kind == "not_found":
            return (f"{entity} not found on the controller (removed or re-adopted?) - "
                    "reselect it in the device settings")
        if exc.kind in ("transport", "server"):
            return (f"controller unreachable or errored ({exc}) - outcome unknown, "
                    "states will update on the next refresh")
        if exc.kind == "shape":
            return f"applied, but the response was unusable - refreshing {entity} info"
        if exc.kind == "bad_request":
            issues = exc.issues
            return "refused by the controller: " + ("; ".join(issues) if issues else str(exc))
        return str(exc)

    def _resolve_camera(self, dev, what):
        """Common precheck for every camera control action: resolve the
        camera id, confirm the plugin is configured, and return the cached
        camera object -- refreshing once if it isn't cached yet. Returns
        (camera_id, info), or None after already logging exactly why
        nothing can proceed, so every action gets identical, specific error
        text instead of duplicating this four times over.

        When the refresh itself fails, ``_refresh_camera_info`` has already
        logged the cause (via ``_describe_api_error``) as its own ERROR
        record -- this method's own message stays generic for that case.
        When the refresh SUCCEEDS but the camera id still isn't in the
        result, that's a different, more specific fact (the camera is
        genuinely gone from the controller, not merely unreachable right
        now) and gets its own distinct wording.
        """
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: {what} - no camera selected.")
            return None
        if not self.api:
            self.logger.error(f"UniFi Protect is not configured; cannot {what.lower()}.")
            return None
        info = self.camera_info.get(camera_id)
        if info is None:
            refreshed = self._refresh_camera_info()
            info = self.camera_info.get(camera_id)
            if info is None:
                if refreshed:
                    self.logger.error(
                        f"{dev.name}: {what} - camera {camera_id} is not on the controller - "
                        "reselect the camera in the device settings."
                    )
                else:
                    self.logger.error(
                        f"{dev.name}: {what} - camera info unavailable, cannot verify capability."
                    )
                return None
        return camera_id, info

    def _patch_camera(self, dev, camera_id, body, what, outcome):
        """Shared PATCH path for every camera control action below.

        ``outcome`` is the human-readable result logged on success (e.g.
        "off" for setStatusLed, "sport" for setVideoMode) -- built by the
        caller, the only one that knows what the PATCH actually meant.

        On success, replaces the cached camera object with the full PATCH
        response and re-applies device state so the issue #4 hardware/
        config states update immediately instead of waiting for the next
        GET /cameras refresh -- that re-apply is itself wrapped, because the
        PATCH already landed on the camera even if Indigo's own state
        update then fails, and that must not look like nothing happened.

        On a refused/errored PATCH, camera_info is left untouched -- the
        error is not new information about the camera's actual state -- and
        described via ``_describe_api_error``. A shape/transport/server
        error additionally triggers a real GET refresh, since the write may
        have applied even though this response couldn't confirm it. Never
        raises.
        """
        try:
            info = self._rest(self.api.patch_camera, camera_id, body)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: {what} - {self._describe_api_error(exc)}")
            if exc.kind in ("shape", "transport", "server"):
                self._refresh_camera_info()
            return
        self.camera_info[camera_id] = info
        try:
            self._apply_camera_state(camera_id, force=True)
        except Exception as exc:  # noqa: BLE001 -- see docstring: must not escape
            self.logger.error(
                f"{dev.name}: {what} applied on the camera, but the Indigo state update "
                f"failed: {type(exc).__name__}: {exc}"
            )
            self.logger.debug("state update after PATCH traceback", exc_info=True)
            return
        self.logger.info(f"{dev.name}: {what} -> {outcome}")

    def setStatusLed(self, action, dev):
        resolved = self._resolve_camera(dev, "Set Status LED")
        if resolved is None:
            return
        camera_id, info = resolved
        if not (info.get("featureFlags") or {}).get("hasLedStatus"):
            self.logger.error(
                f"{dev.name}: Set Status LED - camera does not report a status LED "
                "(featureFlags.hasLedStatus)."
            )
            return
        mode = action.props.get("mode")
        if mode not in LED_MODES:
            self.logger.error(
                f"{dev.name}: Set Status LED - invalid mode {mode!r} "
                f"(must be one of {', '.join(LED_MODES)})."
            )
            return
        if mode == "toggle":
            # The cache can be days stale -- it only ever updates from this
            # plugin's own writes and periodic refreshes, not from changes
            # made in the UniFi app. Re-read before inverting so toggle
            # inverts the camera's REAL current state, not a stale guess.
            try:
                info = self._rest(self.api.get_camera, camera_id)
            except ProtectAPIError as exc:
                self.logger.error(
                    f"{dev.name}: Set Status LED - {self._describe_api_error(exc)}"
                )
                return
            self.camera_info[camera_id] = info
            enabled = not bool((info.get("ledSettings") or {}).get("isEnabled"))
        else:
            enabled = mode == "on"
        self._patch_camera(dev, camera_id, {"ledSettings": {"isEnabled": enabled}},
                            "Set Status LED", "on" if enabled else "off")

    @staticmethod
    def _describe_osd_outcome(osd):
        """"name on, date off" style summary of an osdSettings PATCH body,
        for the success log -- built in the same field order setOsdOverlay
        checks them in, so the order is stable and matches the dialog."""
        labels = (("isNameEnabled", "name"), ("isDateEnabled", "date"),
                  ("isLogoEnabled", "logo"))
        parts = [f"{label} {'on' if osd[key] else 'off'}" for key, label in labels
                 if key in osd]
        if "overlayLocation" in osd:
            parts.append(f"location {osd['overlayLocation']}")
        return ", ".join(parts)

    def setOsdOverlay(self, action, dev):
        resolved = self._resolve_camera(dev, "Set OSD Overlay")
        if resolved is None:
            return
        camera_id, _info = resolved
        props = action.props
        for field, allowed in (("showName", OSD_TOGGLE_VALUES), ("showDate", OSD_TOGGLE_VALUES),
                                ("showLogo", OSD_TOGGLE_VALUES),
                                ("overlayLocation", OVERLAY_LOCATIONS)):
            value = props.get(field)
            if value not in allowed:
                self.logger.error(
                    f"{dev.name}: Set OSD Overlay - invalid {field} {value!r} "
                    f"(must be one of {', '.join(allowed)})."
                )
                return
        osd = {}
        if props["showName"] != "unchanged":
            osd["isNameEnabled"] = props["showName"] == "on"
        if props["showDate"] != "unchanged":
            osd["isDateEnabled"] = props["showDate"] == "on"
        if props["showLogo"] != "unchanged":
            osd["isLogoEnabled"] = props["showLogo"] == "on"
        if props["overlayLocation"] != "unchanged":
            osd["overlayLocation"] = props["overlayLocation"]
        if not osd:
            # validateActionConfigUi rejects this in the dialog, but a
            # scripter can call executeAction directly and skip the dialog
            # entirely -- an empty PATCH is not a no-op worth sending.
            self.logger.error(f"{dev.name}: Set OSD Overlay - nothing selected to change.")
            return
        self._patch_camera(dev, camera_id, {"osdSettings": osd}, "Set OSD Overlay",
                            self._describe_osd_outcome(osd))

    def setVideoMode(self, action, dev):
        resolved = self._resolve_camera(dev, "Set Video Mode")
        if resolved is None:
            return
        camera_id, info = resolved
        video_mode = action.props.get("videoMode", "")
        modes = (info.get("featureFlags") or {}).get("videoModes") or []
        if video_mode not in modes:
            self.logger.error(
                f"{dev.name}: Set Video Mode - '{video_mode}' is not one of this "
                f"camera's supported modes ({', '.join(modes) or 'none reported'})."
            )
            return
        self._patch_camera(dev, camera_id, {"videoMode": video_mode}, "Set Video Mode",
                            video_mode)

    def setHdrMode(self, action, dev):
        resolved = self._resolve_camera(dev, "Set HDR Mode")
        if resolved is None:
            return
        camera_id, info = resolved
        if not (info.get("featureFlags") or {}).get("hasHdr"):
            self.logger.error(
                f"{dev.name}: Set HDR Mode - camera does not support HDR (featureFlags.hasHdr)."
            )
            return
        hdr_type = action.props.get("hdrType")
        if hdr_type not in HDR_TYPES:
            self.logger.error(
                f"{dev.name}: Set HDR Mode - invalid hdrType {hdr_type!r} "
                f"(must be one of {', '.join(HDR_TYPES)})."
            )
            return
        self._patch_camera(dev, camera_id, {"hdrType": hdr_type}, "Set HDR Mode", hdr_type)

    def setMicVolume(self, action, dev):
        resolved = self._resolve_camera(dev, "Set Microphone Volume")
        if resolved is None:
            return
        camera_id, info = resolved
        if not (info.get("featureFlags") or {}).get("hasMic"):
            self.logger.error(
                f"{dev.name}: Set Microphone Volume - camera has no microphone "
                "(featureFlags.hasMic)."
            )
            return
        try:
            volume = int(action.props.get("micVolume", ""))
        except (TypeError, ValueError):
            self.logger.error(f"{dev.name}: Set Microphone Volume - not a whole number.")
            return
        if not 0 <= volume <= 100:
            self.logger.error(f"{dev.name}: Set Microphone Volume - must be 0-100.")
            return
        self._patch_camera(dev, camera_id, {"micVolume": volume}, "Set Microphone Volume",
                            str(volume))

    # -- PTZ (issues #19/#21) ------------------------------------------
    #
    # Fire-and-forget: the official API has no PTZ position readback
    # anywhere, so there is nothing to verify a goto/patrol against after
    # the fact -- a 2xx (204 No Content) is the only confirmation there
    # is. Unlike setStatusLed/setOsdOverlay/etc, these do NOT go through
    # _resolve_camera/_patch_camera (there is no camera object to refresh
    # or cache afterwards) -- the precheck mirrors setChimeVolume's
    # cheaper shape instead: cameraId present, self.api configured.
    # SPEC-DERIVED, UNVERIFIED against the reference rig (no PTZ camera).

    def ptzGotoPreset(self, action, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: PTZ: Go To Preset - no camera selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        slot = action.props.get("slot")
        if slot not in PTZ_SLOTS:
            self.logger.error(
                f"{dev.name}: PTZ: Go To Preset - invalid slot {slot!r} "
                f"(must be one of {', '.join(PTZ_SLOTS)})."
            )
            return
        try:
            self._rest(self.api.ptz_goto, camera_id, int(slot))
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: PTZ: Go To Preset - {self._describe_api_error(exc)}")
            return
        self.logger.info(f"{dev.name}: PTZ: Go To Preset -> slot {slot}")

    def ptzPatrolStart(self, action, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: PTZ: Start Patrol - no camera selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        slot = action.props.get("slot")
        if slot not in PTZ_SLOTS:
            self.logger.error(
                f"{dev.name}: PTZ: Start Patrol - invalid slot {slot!r} "
                f"(must be one of {', '.join(PTZ_SLOTS)})."
            )
            return
        try:
            self._rest(self.api.ptz_patrol_start, camera_id, int(slot))
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: PTZ: Start Patrol - {self._describe_api_error(exc)}")
            return
        self.logger.info(f"{dev.name}: PTZ: Start Patrol -> slot {slot}")

    def ptzPatrolStop(self, action, dev):
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: PTZ: Stop Patrol - no camera selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            self._rest(self.api.ptz_patrol_stop, camera_id)
        except ProtectAPIError as exc:
            self.logger.error(f"{dev.name}: PTZ: Stop Patrol - {self._describe_api_error(exc)}")
            return
        self.logger.info(f"{dev.name}: PTZ: Stop Patrol -> stopped")

    def refreshCameras(self, action):
        self._refresh_camera_info()

    # -- Alarm Manager webhook (issue #21) ------------------------------
    #
    # Plugin-level, not a device action -- one Protect console has one
    # Alarm Manager, independent of any single camera. SPEC-DERIVED,
    # UNVERIFIED against the reference rig (no Alarm Manager alarms
    # configured there).

    def triggerAlarmWebhook(self, action):
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        raw = action.props.get("webhookId", "")
        trigger_id = self.substitute(raw)
        if not trigger_id or not trigger_id.strip():
            self.logger.error(
                "Trigger Alarm Manager Webhook - webhook trigger ID is empty "
                "(after variable substitution)."
            )
            return
        try:
            self._rest(self.api.send_alarm_webhook, trigger_id)
        except ProtectAPIError as exc:
            self.logger.error(f"Trigger Alarm Manager Webhook - {self._describe_api_error(exc)}")
            return
        self.logger.info("Trigger Alarm Manager Webhook -> sent")

    def discoverCameras(self):
        """Menu id/callback kept as 'discoverCameras' for compatibility
        (Devices.xml's <Name> now reads "Discover Devices"); issue #8
        extends this to list every device class, not just cameras."""
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        self._discover_cameras()
        self._discover_sensors()
        self._discover_lights()
        self._discover_chimes()
        self._discover_nvr()

    def _discover_cameras(self):
        try:
            cameras = self._rest(self.api.get_cameras)
        except ProtectAPIError as exc:
            self.logger.error(f"Camera discovery failed: {exc}")
            return
        known = set(self.cameras)
        self.logger.info(f"Protect reports {len(cameras)} camera(s):")
        for cam in cameras:
            flag = "[in Indigo]" if cam.get("id") in known else "[not yet added]"
            self.logger.info(
                f"  {cam.get('name')} - {cam.get('type')} - {cam.get('state')} {flag}"
            )

    def _discover_sensors(self):
        try:
            sensors = self._rest(self.api.get_sensors)
        except ProtectAPIError as exc:
            self.logger.error(f"Sensor discovery failed: {exc}")
            return
        known = set(self.sensors)
        self.logger.info(f"Protect reports {len(sensors)} sensor(s):")
        for sensor in sensors:
            flag = "[in Indigo]" if sensor.get("id") in known else "[not yet added]"
            self.logger.info(
                f"  {sensor.get('name')} - {sensor.get('mountType')} - "
                f"{sensor.get('state')} {flag}"
            )

    def _discover_lights(self):
        try:
            lights = self._rest(self.api.get_lights)
        except ProtectAPIError as exc:
            self.logger.error(f"Light discovery failed: {exc}")
            return
        known = set(self.lights)
        self.logger.info(f"Protect reports {len(lights)} light(s):")
        for light in lights:
            flag = "[in Indigo]" if light.get("id") in known else "[not yet added]"
            self.logger.info(f"  {light.get('name')} - {light.get('state')} {flag}")

    def _discover_chimes(self):
        try:
            chimes = self._rest(self.api.get_chimes)
        except ProtectAPIError as exc:
            self.logger.error(f"Chime discovery failed: {exc}")
            return
        known = set(self.chimes)
        self.logger.info(f"Protect reports {len(chimes)} chime(s):")
        for chime in chimes:
            flag = "[in Indigo]" if chime.get("id") in known else "[not yet added]"
            self.logger.info(f"  {chime.get('name')} - {chime.get('state')} {flag}")

    def _discover_nvr(self):
        try:
            nvr = self._rest(self.api.get_nvr)
        except ProtectAPIError as exc:
            self.logger.error(f"NVR discovery failed: {exc}")
            return
        flag = "[in Indigo]" if self.nvrs else "[not yet added]"
        self.logger.info(f"Protect NVR: {nvr.get('name')} {flag}")

    def toggleDebug(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

    # ------------------------------------------------------------------
    # Stream URLs (issue #7) -- opt-in, never logged
    #
    # The RTSPS URL embeds an access token: treat it like a credential.
    # Turning the per-device checkbox off must remove it from the Indigo
    # database, not merely stop refreshing it.
    #
    # REST for this feature happens ONLY from _drain_one_pending_stream_
    # refresh (called once per _pump tick) or from a user-initiated,
    # synchronous call (the refreshStreamUrls action, Send Status Request).
    # deviceStartComm and _open_socket only ever call the cheap, REST-free
    # _prime_stream_urls -- both run somewhere a >= 3s-per-call REST throttle
    # cannot be allowed to block (the main thread, or ahead of _pump).
    # ------------------------------------------------------------------

    def refreshStreamUrls(self, action, dev):
        self._refresh_stream_urls_sync(dev)

    def deleteStreamUrls(self, action, dev):
        """Delete the selected RTSPS stream qualities on the controller
        (issue #25). One quality at a time (protect_api.delete_rtsps_stream
        only ever deletes one) -- a single quality failing must not skip
        the rest, so each is tried and reported independently.

        SPEC-DERIVED, UNVERIFIED against the reference rig. Streams are
        NEVER deleted automatically anywhere else in this plugin -- this
        action is the only path that removes one.
        """
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(f"{dev.name}: Delete Stream URLs - no camera selected.")
            return
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        selected = [quality for quality in RTSPS_DELETE_QUALITIES
                    if _truthy(action.props.get(quality), default=False)]
        if not selected:
            self.logger.error(f"{dev.name}: Delete Stream URLs - no qualities selected.")
            return

        deleted = []
        failed = []
        for quality in selected:
            try:
                self._rest(self.api.delete_rtsps_stream, camera_id, quality)
            except ProtectAPIError as exc:
                if exc.kind == "not_found":
                    self.logger.info(
                        f"{dev.name}: Delete Stream URLs - {quality} was already gone "
                        "on the controller."
                    )
                else:
                    self.logger.error(
                        f"{dev.name}: Delete Stream URLs - {quality}: "
                        f"{self._describe_api_error(exc)}"
                    )
                    failed.append(quality)
                    continue
            dev.updateStatesOnServer([{"key": STREAM_URL_STATES[quality], "value": ""}])
            deleted.append(quality)

        summary = []
        if deleted:
            summary.append(f"deleted: {', '.join(deleted)}")
        if failed:
            summary.append(f"failed: {', '.join(failed)}")
        self.logger.info(f"{dev.name}: Delete Stream URLs -> {'; '.join(summary)}")

        if deleted and self._stream_urls_exposed(dev):
            self.logger.warning(
                f"{dev.name}: 'Expose RTSPS stream URLs' is still ticked for this device "
                "- the plugin will recreate the deleted stream(s) the next time it "
                "refreshes stream URLs for this camera (device restart, event-socket "
                "reconnect, or the Refresh Stream URLs action)."
            )

    def _stream_urls_exposed(self, dev):
        expose = dev.pluginProps.get("exposeStreamUrls", False)
        if isinstance(expose, str):
            expose = expose.strip().lower() == "true"
        return bool(expose)

    def _clear_stream_urls(self, dev):
        dev.updateStatesOnServer(
            [{"key": key, "value": ""} for key in STREAM_URL_STATES.values()])

    def _current_stream_state_values(self, dev):
        return {quality: dev.states.get(key, "") for quality, key in STREAM_URL_STATES.items()}

    def _prime_stream_urls(self, dev):
        """Cheap, REST-free half of the stream-URL lifecycle. Off: clears
        the four states immediately, so unticking the checkbox removes the
        token from the database without waiting for a pump tick. On:
        queues the device for _drain_one_pending_stream_refresh."""
        if not self._stream_urls_exposed(dev):
            self._clear_stream_urls(dev)
            return
        self._stream_refresh_pending.add(dev.id)

    def _drain_one_pending_stream_refresh(self):
        """Pop at most one pending device id and refresh its stream URLs.

        Wrapped broadly on purpose: an AssertionError from the leak guard,
        or an Indigo write error, must never reach runConcurrentThread's
        generic exception handler -- that would tear the whole event socket
        down and reconnect-loop forever over what is, at worst, one broken
        camera's stream URLs. ProtectAPIError is already handled (and
        logged) inside _refresh_stream_urls without raising; this catches
        everything else.
        """
        if not self._stream_refresh_pending:
            return
        dev_id = self._stream_refresh_pending.pop()
        dev = indigo.devices.get(dev_id, None)
        if dev is None or not dev.enabled:
            return
        camera_id = dev.pluginProps.get("cameraId", "")
        if dev.id not in self.cameras.get(camera_id, ()):
            return  # no longer mapped -- device re-pointed or removed
        try:
            self._refresh_stream_urls(dev)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error(
                f"{dev.name}: stream URL refresh failed ({type(exc).__name__}: {exc}) - "
                "untick 'Expose RTSPS stream URLs' on this device if it persists."
            )

    def _refresh_stream_urls_sync(self, dev):
        """User-initiated refresh (the refreshStreamUrls action, and Send
        Status Request via actionControlUniversal) -- there is a user
        watching the Event Log for the result, so every outcome is
        reported, never silent, unlike the pump-drain path."""
        if not self._stream_urls_exposed(dev):
            self._clear_stream_urls(dev)
            self.logger.info(
                f"{dev.name}: stream URLs are not exposed for this device - tick "
                "'Expose RTSPS stream URLs' in the device settings."
            )
            return
        if self.api is None:
            self.logger.error(
                "UniFi Protect is not configured; cannot refresh stream URLs."
            )
            return
        self._refresh_stream_urls(dev)

    def _clean_stream_response(self, dev, response, current):
        """Validate one rtsps-stream response body against the four known
        quality keys. A key present with a non-string, non-null value is a
        shape surprise, not real data -- skip it with a WARNING naming it,
        rather than trust it. A key absent from the response is simply
        absent from the returned dict, identically to an explicit null, for
        every caller below."""
        cleaned = {}
        for quality in STREAM_URL_STATES:
            if quality not in response:
                continue
            value = response[quality]
            if value is not None and not isinstance(value, str):
                self.logger.warning(_assert_no_url_in_message(
                    f"{dev.name}: stream URL response had a non-string value for "
                    f"'{quality}' - skipping it", current))
                continue
            cleaned[quality] = value
        return cleaned

    def _log_stream_error(self, dev, exc, current):
        note = "the stored URLs may now be stale" if any(current.values()) \
            else "no URLs are stored yet"
        self.logger.error(_assert_no_url_in_message(
            f"{dev.name}: could not refresh stream URLs "
            f"({self._describe_api_error(exc)}) - {note}.", current))

    def _fill_missing_stream_qualities(self, dev, camera_id, cleaned, missing, current):
        """POST to create the qualities still missing after GET, but only
        when this camera's capabilities are cached -- a guessed POST before
        the first camera refresh could request a quality the camera
        doesn't support. Split out of _refresh_stream_urls because pylint
        already flags that method for too-many-locals/branches (the same
        reason _camera_info_states was split out of _write_states).

        Returns the (possibly merged) `cleaned` dict, or None if a
        ProtectAPIError was already logged and the caller should abort
        without writing any state.
        """
        if camera_id not in self.camera_info:
            self.logger.warning(
                f"{dev.name}: camera capabilities not loaded yet - streams "
                "will be created on the next refresh."
            )
            return cleaned
        try:
            created = self._rest(self.api.create_rtsps_streams, camera_id, missing)
        except ProtectAPIError as exc:
            self._log_stream_error(dev, exc, current)
            return None
        created_cleaned = self._clean_stream_response(dev, created, current)
        for quality in missing:
            if created_cleaned.get(quality) is not None:
                cleaned[quality] = created_cleaned[quality]
        return cleaned

    def _refresh_stream_urls(self, dev):
        """Fetch and write the four RTSPS stream-URL states for one camera
        device. Callers on the async/pump path have no user to report to,
        so the opt-out and self.api-is-None cases are re-checked here
        defensively and handled silently; _refresh_stream_urls_sync handles
        them loudly before ever calling in.

        GET the current streams; validate the response shape and per-key
        types; for each quality still missing (null/absent/invalid) that is
        actually wanted (package only when the cached camera reports
        hasPackageCamera), POST to create it -- but only when this camera's
        capabilities are cached at all, since a guessed POST before the
        first camera refresh could request a quality the camera doesn't
        support. A quality that is still empty after all that falls back to
        whatever was already stored, rather than blanking a URL a viewer
        may be actively using; only a quality with nothing stored either
        becomes "". Every log line here -- success, warning, or error --
        goes through _assert_no_url_in_message against both the fresh and
        the current values, since the error path has no fresh response to
        check.
        """
        if not self._stream_urls_exposed(dev):
            self._clear_stream_urls(dev)
            return
        if self.api is None:
            return

        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            return

        current = self._current_stream_state_values(dev)

        try:
            streams = self._rest(self.api.get_rtsps_streams, camera_id)
        except ProtectAPIError as exc:
            self._log_stream_error(dev, exc, current)
            return

        if not any(quality in streams for quality in STREAM_URL_STATES):
            self.logger.error(_assert_no_url_in_message(
                f"{dev.name}: unexpected response shape for stream URLs - expected "
                "high/medium/low/package", current))
            return

        cleaned = self._clean_stream_response(dev, streams, current)

        wanted = ["high", "medium", "low"]
        info = self.camera_info.get(camera_id)
        if info and info.get("hasPackageCamera"):
            wanted.append("package")
        missing = [quality for quality in wanted if cleaned.get(quality) is None]

        if missing:
            cleaned = self._fill_missing_stream_qualities(dev, camera_id, cleaned, missing, current)
            if cleaned is None:
                return

        final = {}
        kept = []
        for quality in STREAM_URL_STATES:
            value = cleaned.get(quality)
            if value:
                final[quality] = value
            elif current.get(quality):
                final[quality] = current[quality]
                kept.append(quality)
            else:
                final[quality] = ""

        if kept:
            self.logger.warning(_assert_no_url_in_message(
                f"{dev.name}: stream URL refresh kept previous URL for: "
                f"{', '.join(sorted(kept))}", current, final))

        present = sorted(quality for quality, value in final.items() if value)
        self.logger.info(_assert_no_url_in_message(
            f"{dev.name}: stream URLs refreshed ({', '.join(present)})", current, final))

        dev.updateStatesOnServer([
            {"key": key, "value": final[quality]}
            for quality, key in STREAM_URL_STATES.items()
        ])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_camera_info(self):
        """Refresh self.camera_info from a real GET /cameras. Returns True
        on success, False on any failure (including "not configured") --
        callers that need to distinguish "the id isn't in a fresh read" from
        "the read itself failed" (e.g. _resolve_camera) rely on this."""
        if not self.api:
            return False
        try:
            cameras = self._rest(self.api.get_cameras)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not refresh cameras: {self._describe_api_error(exc)}")
            # Deliberately leave camera_info untouched: replacing good data with
            # {} would silently downgrade every cameraState to "unavailable".
            return False
        self.camera_info = {c["id"]: c for c in cameras if c.get("id")}
        self.logger.debug(f"refreshed {len(self.camera_info)} camera(s)")
        return True

    def _rest(self, func, *args, **kwargs):
        """Call a ProtectAPI method, never faster than MIN_REST_INTERVAL.

        The controller answers 429 under a burst, and a 429 during discovery
        looks exactly like 'no cameras' to a caller that isn't careful.
        """
        gap = time.monotonic() - self._last_rest_call
        if gap < MIN_REST_INTERVAL:
            self._throttle_sleep(MIN_REST_INTERVAL - gap)
        try:
            return func(*args, **kwargs)
        finally:
            self._last_rest_call = time.monotonic()

    def _throttle_sleep(self, seconds):
        """self.sleep() on the concurrent thread so StopThread is honoured;
        time.sleep() elsewhere, because self.sleep() would raise StopThread
        inside a UI callback where that is meaningless."""
        try:
            self.sleep(seconds)
        except self.StopThread:
            raise
        except Exception:
            time.sleep(seconds)
