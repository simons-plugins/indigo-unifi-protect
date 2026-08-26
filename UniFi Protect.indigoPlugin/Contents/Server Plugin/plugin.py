#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UniFi Protect plugin for Indigo.

Motion state is driven entirely by the Protect event WebSocket. There is no
polling fallback and this is not an oversight: the integration API's camera
object carries no motion field and `GET /events` returns 404.

What that costs, stated plainly because it shapes every automation written
against this plugin: Indigo booleans cannot express "unknown". When the socket
is down this plugin forces `motionDetected` False and `connected` False. It
does NOT have a way to say "I cannot tell" in the motion state itself.

    Any trigger that acts on motion MUST gate on `connected` first.

Everything below exists to make `connected` trustworthy, because it is the only
signal that separates "nobody is there" from "I have no idea".
"""

import os
import time
from datetime import datetime

import indigo

from event_tracker import EventTracker
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

# Written to cameraState when the camera list could not be fetched. An empty
# string is indistinguishable from "the camera genuinely reports no state", and
# a trigger reading it would see "not DISCONNECTED" and believe things are fine.
STATE_UNAVAILABLE = "unavailable"


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
        self._resources_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Resources")
        )

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
        automations are expected to gate on the latter."""
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
        active = self.tracker.is_active(camera_id) if connected else False
        types = sorted(self.tracker.detect_types(camera_id)) if connected else []
        info = self.camera_info.get(camera_id)

        states = [
            {"key": "onOffState", "value": active},
            {"key": "motionDetected", "value": active},
            {"key": "lastDetectTypes", "value": ",".join(types)},
            {"key": "cameraState", "value": info.get("state", "") if info else STATE_UNAVAILABLE},
            {"key": "connected", "value": connected},
        ]
        for detect_type in TRACKED_DETECT_TYPES:
            states.append({"key": f"{detect_type}Detected", "value": detect_type in types})

        last_ms = self.tracker.last_motion_ms(camera_id)
        if isinstance(last_ms, (int, float)) and last_ms > 0:
            states.append({
                "key": "lastMotion",
                "value": datetime.fromtimestamp(last_ms / 1000.0).isoformat(timespec="seconds"),
            })

        if force or active != bool(dev.states.get("onOffState", False)):
            dev.updateStateImageOnServer(
                indigo.kStateImageSel.MotionSensorTripped if active
                else indigo.kStateImageSel.MotionSensor
            )
        dev.updateStatesOnServer(states)

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

        os.makedirs(self._resources_dir, exist_ok=True)
        path = os.path.join(self._resources_dir, f"camera_{dev.id}.jpg")
        tmp = f"{path}.tmp"
        # Write via a temp file so a control page never serves a half-written JPEG.
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

        dev.updateStateOnServer("snapshotPath", value=path)
        self.logger.info(f"{dev.name}: snapshot saved ({len(data)} bytes)")

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
        if not self.api:
            return
        try:
            cameras = self._rest(self.api.get_cameras)
        except ProtectAPIError as exc:
            self.logger.error(f"Could not refresh cameras: {exc}")
            # Deliberately leave camera_info untouched: replacing good data with
            # {} would silently downgrade every cameraState to "unavailable".
            return
        self.camera_info = {c["id"]: c for c in cameras if c.get("id")}
        self.logger.debug(f"refreshed {len(self.camera_info)} camera(s)")

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
