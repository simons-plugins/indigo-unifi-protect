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

from event_tracker import EventTracker, KNOWN_UNSUPPORTED_EVENT_TYPES, MISSING_TYPE_KEY
from protect_api import ProtectAPI, ProtectAPIError
from protect_ws import ProtectEventSocket

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

# Under Indigo's "Web Assets/images", so snapshots survive plugin upgrades and
# are servable to control pages at /images/<SNAPSHOT_SUBDIR>/...
SNAPSHOT_SUBDIR = "unifi-protect"


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

        # Protect camera id -> set of Indigo device ids. A set, not a scalar:
        # two Indigo devices pointed at one camera is trivially produced by the
        # Duplicate command, and a 1:1 dict silently freezes the loser forever.
        self.cameras = {}
        self.camera_info = {}

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
        camera_id = dev.pluginProps.get("cameraId", "")
        if not camera_id:
            self.logger.error(
                f"{dev.name}: no camera selected - edit the device settings and pick one."
            )
            return
        self.cameras.setdefault(camera_id, set()).add(dev.id)
        # connected is DERIVED, never assumed. At this point in the lifecycle
        # Indigo has not yet started runConcurrentThread, so there is no socket
        # and the honest answer is False.
        self._apply_camera_state(camera_id, force=True)

    def deviceStopComm(self, dev):
        # Remove by device id, not by the camera id in props: if the user just
        # edited the device to point at a different camera, the props already
        # hold the NEW id and popping by it would orphan the old mapping.
        for camera_id in list(self.cameras):
            self.cameras[camera_id].discard(dev.id)
            if not self.cameras[camera_id]:
                del self.cameras[camera_id]

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        if not valuesDict.get("cameraId", "").strip():
            errors["cameraId"] = (
                "No camera selected. If the list was empty or showed an error, the "
                "camera list could not be loaded - check the Event Log, then close "
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

    def _pump(self):
        last_ping = time.monotonic()
        while True:
            # Only self.sleep() raises StopThread, and this loop blocks in
            # recv() rather than sleeping -- so check the flag explicitly or a
            # shutdown hangs until the socket happens to die.
            if self.stopThread:
                raise self.StopThread
            if self._reconnect_requested:
                self._reconnect_requested = False
                raise ConnectionError("reconnect requested after a configuration change")

            message = self.socket.read_message(timeout=1.0)
            now = time.monotonic()

            if message is not None:
                changed = self.tracker.handle(message)
                for camera_id in changed:
                    self._apply_camera_state(camera_id)
                self._report_dropped_frames()
                self._report_ignored_types()

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
        expected to gate on `connected`."""
        for camera_id in list(self.cameras):
            self.tracker.clear_camera(camera_id)
            self._apply_camera_state(camera_id, connected=False, force=True)

    # ------------------------------------------------------------------
    # State writing
    # ------------------------------------------------------------------

    def _is_connected(self):
        # self.socket is only ever set after a successful handshake (_open_socket)
        # and is cleared by _close_socket, so its presence IS connectedness.
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
    # Actions and menu items
    # ------------------------------------------------------------------

    def actionControlUniversal(self, action, dev):
        """Devices.xml sets SupportsStatusRequest, so Indigo shows "Send Status
        Request" on every camera. Declaring the capability and then doing
        nothing is worse than not declaring it -- this is the first thing a user
        tries on a sensor that looks stuck."""
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            camera_id = dev.pluginProps.get("cameraId", "")
            if not camera_id:
                self.logger.error(f"{dev.name}: no camera selected.")
                return
            self._refresh_camera_info()
            self._apply_camera_state(camera_id, force=True)
            self.logger.info(
                f"{dev.name}: refreshed (event socket "
                f"{'connected' if self._is_connected() else 'DOWN'})"
            )

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
    def _describe_api_error(exc):
        """One line of actionable text for a ProtectAPIError, keyed on
        ``exc.kind`` -- shared by every camera control action's error log
        plus ``_refresh_camera_info``, so what a given failure MEANS is
        answered identically everywhere instead of every call site
        formatting its own ``str(exc)`` HTTP-status-and-body dump.
        Deliberately NOT used by ``takeSnapshot``/``discoverCameras``,
        whose existing wording predates this and is untouched.

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
            return ("camera not found on the controller (removed or re-adopted?) - "
                    "reselect it in the device settings")
        if exc.kind in ("transport", "server"):
            return (f"controller unreachable or errored ({exc}) - outcome unknown, "
                    "states will update on the next refresh")
        if exc.kind == "shape":
            return "applied, but the response was unusable - refreshing camera info"
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

    def refreshCameras(self, action):
        self._refresh_camera_info()

    def discoverCameras(self):
        if not self.api:
            self.logger.error("UniFi Protect is not configured.")
            return
        try:
            cameras = self._rest(self.api.get_cameras)
        except ProtectAPIError as exc:
            self.logger.error(f"Discovery failed: {exc}")
            return
        known = set(self.cameras)
        self.logger.info(f"Protect reports {len(cameras)} camera(s):")
        for cam in cameras:
            flag = "[in Indigo]" if cam.get("id") in known else "[not yet added]"
            self.logger.info(
                f"  {cam.get('name')} - {cam.get('type')} - {cam.get('state')} {flag}"
            )

    def toggleDebug(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

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
