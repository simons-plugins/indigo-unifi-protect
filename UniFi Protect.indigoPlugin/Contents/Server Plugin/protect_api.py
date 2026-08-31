"""Stdlib-only HTTP client for the UniFi Protect integration API.

Talks to ``https://<host>/proxy/protect/integration/v1`` using a single
``X-API-KEY`` header (no login, no cookies, no CSRF token). Built entirely
on :mod:`urllib.request`; no third-party HTTP library.
"""

from __future__ import annotations

import email.utils
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

_logger = logging.getLogger(__name__)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header value: either delay-seconds or an
    HTTP-date. Returns ``None`` when absent or unparseable.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    now = email.utils.parsedate_to_datetime(email.utils.formatdate(usegmt=True))
    delta = (dt - now).total_seconds()
    return max(delta, 0.0)


def _assert_no_secret(text: str, secret: str) -> str:
    """Defensive guard: fail loudly if the API key ever leaks into text.

    Called on every *constructed* exception/log message this module builds
    (the ``f"HTTP {code} for {path}"`` style strings). It does NOT cover:

    - ``ProtectAPIError.body`` -- the raw server response text, passed
      through unchecked. The API key travels only in the request header,
      never in the response body, so this is not expected to leak it, but
      the guard does not verify that.
    - the warning logged in ``get_snapshot`` on the high-quality fallback --
      it is built from ``camera_id`` only, with no access to the key.

    ``secret`` is the empty string only in tests that construct messages
    without a key; the check is skipped in that case since there is nothing
    to leak.
    """
    if secret and secret in text:
        raise AssertionError("API key must never appear in logged/exception text")
    return text


class ProtectAPIError(Exception):
    """Raised for any non-2xx response or transport failure.

    Attributes:
        status: HTTP status code, or ``None`` for a transport-level failure
            (connection refused, timeout, TLS error, malformed body, etc).
        body: Response body text, or a short description of the transport
            failure when ``status`` is ``None``.
        url: The request URL. Never contains the API key -- the key travels
            only in the ``X-API-KEY`` header, never in the URL.
        kind: Coarse failure category, either derived from ``status`` or
            passed explicitly by the raiser:
            "auth" (401/403), "not_found" (404), "rate_limited" (429),
            "bad_request" (400), "server" (5xx), "transport" (status is
            None), "http" (any other non-2xx status), "shape" (the
            response parsed as JSON but was not the expected shape --
            passed explicitly, never derived from ``status``).
        retry_after: Seconds to wait before retrying, parsed from the
            response's ``Retry-After`` header when present (most relevant
            for ``kind == "rate_limited"``). ``None`` when absent or
            unparseable.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = "", url: str = "",
                 retry_after: Optional[float] = None,
                 kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url
        self.retry_after = retry_after
        # An explicit `kind` (e.g. "shape" for a response that parsed as
        # JSON but was the wrong shape) overrides the status-code
        # classification below -- a shape mismatch is not really an HTTP
        # problem and callers that branch on `kind` need to tell the two
        # apart.
        self.kind = kind if kind is not None else self._classify(status)

    @property
    def issues(self) -> list[str]:
        """Field-level validation issues from an AJV_PARSE_ERROR body, e.g.

            {"issues": [{"instancePath": "/videoMode",
                         "message": "must be equal to one of the allowed values"}]}

        Formatted as ``"<instancePath>: <message>"`` per issue
        (``instancePath`` defaults to ``"/"`` when empty). Lazily computed
        from ``body`` on every access rather than cached -- this is a
        rarely-read diagnostic, not a hot path.

        When ``body`` has no usable ``issues`` list but does have a string
        ``error`` (e.g. a 404's ``{"error":"Entity 'camera' not found",
        "name":"NOT_FOUND"}``), returns ``[error]`` -- the controller's own
        one-line explanation is still worth surfacing even outside the AJV
        shape. Returns ``[]`` only when ``body`` isn't a JSON object, or is
        one with neither an ``issues`` list nor a string ``error`` (a body
        the server didn't send as JSON, or one with nothing to say).
        """
        try:
            parsed = json.loads(self.body)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, dict):
            return []
        issues = parsed.get("issues")
        if not isinstance(issues, list):
            error = parsed.get("error")
            return [error] if isinstance(error, str) and error else []
        result = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            path = issue.get("instancePath") or "/"
            result.append(f"{path}: {issue.get('message', '')}")
        return result

    @staticmethod
    def _classify(status: Optional[int]) -> str:
        if status is None:
            return "transport"
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limited"
        if status == 400:
            return "bad_request"
        if 500 <= status < 600:
            return "server"
        return "http"


def _redact_body(exc: ProtectAPIError) -> ProtectAPIError:
    """Return a copy of ``exc`` with ``body=""``, everything else preserved
    (including ``kind``, so a caller's ``exc.kind == "auth"`` branching is
    unaffected). Used by the RTSPS stream endpoints: ``_request`` puts the
    real HTTP-error response text into ``.body``, but a live-stream URL (an
    access token) could plausibly appear in either endpoint's body, and
    that must never be readable off the exception.
    """
    return ProtectAPIError(str(exc), status=exc.status, body="", url=exc.url,
                            retry_after=exc.retry_after, kind=exc.kind)


class ProtectAPI:
    """Blocking HTTP client for the UniFi Protect integration REST API."""

    def __init__(self, host: str, api_key: str, verify_ssl: bool = False,
                 timeout: int = 15) -> None:
        self._host = host
        self._api_key = api_key
        self._timeout = timeout
        self._base_url = f"https://{host}/proxy/protect/integration/v1"

        self._ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _request(self, method: str, path: str, params: Optional[dict[str, str]] = None,
                 body: Optional[dict] = None) -> bytes:
        """Issue an HTTP request and return the raw response body.

        ``body``, when given, is JSON-encoded and sent with a
        ``Content-Type: application/json`` / ``Accept: application/json``
        request -- used by ``patch_camera``. ``_get`` is a thin wrapper over
        this with no body, kept as its own method so existing callers and
        tests are unaffected.

        Raises ProtectAPIError on any non-2xx response or transport failure.
        """
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"X-API-KEY": self._api_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout,
                                         context=self._ctx) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            resp_body = exc.read().decode("utf-8", errors="replace")
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            message = _assert_no_secret(f"HTTP {exc.code} for {path}", self._api_key)
            raise ProtectAPIError(message, status=exc.code, body=resp_body, url=url,
                                   retry_after=retry_after) from None
        except urllib.error.URLError as exc:
            message = _assert_no_secret(
                f"Connection failure for {path}: {exc.reason}", self._api_key)
            raise ProtectAPIError(message, status=None, body=str(exc.reason), url=url) from None
        except OSError as exc:
            message = _assert_no_secret(f"Connection failure for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body=str(exc), url=url) from None

    def _get(self, path: str, params: Optional[dict[str, str]] = None) -> bytes:
        """Issue a GET request and return the raw response body.

        Raises ProtectAPIError on any non-2xx response or transport failure.
        """
        return self._request("GET", path, params=params)

    def _get_json(self, path: str, params: Optional[dict[str, str]] = None) -> Any:
        raw = self._get(path, params=params)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = _assert_no_secret(f"Invalid JSON response for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body=raw[:200].decode(
                "utf-8", errors="replace"), url=f"{self._base_url}{path}") from None

    def get_cameras(self) -> list[dict]:
        """GET /cameras. Raises ProtectAPIError, including when the parsed
        body is not a JSON array of objects (e.g. a proxy error page or an
        API shape change) -- callers can rely on the return value actually
        being ``list[dict]``, never a bare ``dict`` or other JSON shape.
        """
        path = "/cameras"
        body = self._get_json(path)
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected a list of objects",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}")
        return body

    def get_camera(self, camera_id: str) -> dict:
        """GET /cameras/{id}. Raises ProtectAPIError, including when the
        parsed body is not a JSON object.
        """
        path = f"/cameras/{camera_id}"
        body = self._get_json(path)
        if not isinstance(body, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}")
        return body

    def patch_camera(self, camera_id: str, body: dict) -> dict:
        """PATCH /cameras/{id}. A partial body is accepted; the response is
        the FULL camera object (same shape as ``get_camera``), which callers
        use to refresh their cached copy without a separate GET.

        Raises ProtectAPIError, including:

        - when the parsed body is not a JSON object -- callers can rely on
          the return value actually being a ``dict``, mirroring
          ``get_camera``.
        - when it IS a dict but not recognizably the camera object --
          checked as ``parsed.get("id") == camera_id and
          isinstance(parsed.get("featureFlags"), dict)``. A 200 that passes
          the plain dict check but fails this one (e.g. a proxy wrapper like
          ``{"id": "cam-1"}`` with none of the real fields) would otherwise
          replace the caller's cache with a near-empty object, blanking
          every hardware state and making every later capability gate lie.
          Raised with ``kind="shape"`` so callers can react distinctly from
          an actual 400/404 refusal.

        On a 400 (bad request), the server's AJV validation issues -- if
        any -- are on the raised error's ``issues`` property.
        """
        path = f"/cameras/{camera_id}"
        raw = self._request("PATCH", path, body=body)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = _assert_no_secret(f"Invalid JSON response for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body=raw[:200].decode(
                "utf-8", errors="replace"), url=f"{self._base_url}{path}") from None
        if not isinstance(parsed, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(parsed)[:200],
                                   url=f"{self._base_url}{path}")
        if not (parsed.get("id") == camera_id and isinstance(parsed.get("featureFlags"), dict)):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected the camera object",
                self._api_key)
            raise ProtectAPIError(message, status=None, kind="shape",
                                   body=str(parsed)[:200], url=f"{self._base_url}{path}")
        return parsed

    def get_snapshot(self, camera_id: str, high_quality: bool = False,
                      supports_high_quality: Optional[bool] = None) -> bytes:
        """Return raw JPEG bytes for the camera's current snapshot.

        Raises ProtectAPIError. If ``high_quality`` is requested:

        - When the caller knows the camera's capability (typically from its
          cached ``featureFlags.supportFullHdSnapshot``), pass it as
          ``supports_high_quality``. If False, ``highQuality`` is never
          requested in the first place -- no wasted round trip, no prose
          matching.
        - When ``supports_high_quality`` is left as ``None`` (capability
          unknown), fall back to requesting ``highQuality`` and, as a
          last-resort heuristic, retrying ONCE without the flag if the
          server answers 400 mentioning "full hd" -- but log a warning
          naming the camera. This prose match is fragile (a server-side
          wording change silently stops the retry) and exists only for
          callers that cannot supply ``supports_high_quality``. Any other
          400 (or any error from the retry itself) propagates unchanged.
        """
        want_high_quality = high_quality and supports_high_quality is not False
        params = {"highQuality": "true"} if want_high_quality else None
        try:
            return self._check_jpeg(self._get(f"/cameras/{camera_id}/snapshot",
                                              params=params), camera_id)
        except ProtectAPIError as exc:
            if (want_high_quality and supports_high_quality is None
                    and exc.status == 400 and "full hd" in exc.body.lower()):
                _logger.warning(
                    "Camera %s does not support full HD snapshot; retrying without highQuality",
                    camera_id)
                return self._check_jpeg(
                    self._get(f"/cameras/{camera_id}/snapshot"), camera_id)
            raise

    @staticmethod
    def _check_jpeg(data: bytes, camera_id: str) -> bytes:
        """Reject a body that is not actually a JPEG.

        A 200 carrying an empty body, or an HTML error page from a proxy, would
        otherwise be written to disk as a .jpg and logged as
        "snapshot saved (0 bytes)" -- a success message for a failed call. An
        unusable result is a failed call, not an empty one.
        """
        if not data:
            raise ProtectAPIError(
                f"Empty snapshot body for camera {camera_id} - the controller "
                "returned 200 with no image data")
        if not data.startswith(b"\xff\xd8\xff"):
            raise ProtectAPIError(
                f"Snapshot for camera {camera_id} is not a JPEG "
                f"(starts with {data[:8]!r}) - the controller may have returned "
                "an error page")
        return data

    def get_rtsps_streams(self, camera_id: str) -> dict:
        """GET /cameras/{id}/rtsps-stream. Raises ProtectAPIError, including
        when the parsed body is not a JSON object.

        Every ProtectAPIError raised here -- an HTTP-error/transport
        failure from ``_get_json`` (unlike most callers, ``_request`` puts
        the real response text into ``.body`` for those) or this method's
        own shape check -- carries ``body=""`` instead: this endpoint's
        response can contain a live-stream URL (an access token), and a
        malformed body could echo one back through the exception.
        """
        path = f"/cameras/{camera_id}/rtsps-stream"
        try:
            body = self._get_json(path)
        except ProtectAPIError as exc:
            raise _redact_body(exc) from None
        if not isinstance(body, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body="",
                                   url=f"{self._base_url}{path}")
        return body

    def create_rtsps_streams(self, camera_id: str, qualities: list[str]) -> dict:
        """POST /cameras/{id}/rtsps-stream to create RTSPS streams for the
        requested qualities (e.g. ``["high", "medium", "low"]``). Returns
        the same four-key object ``get_rtsps_streams`` returns.

        Unverified against the reference rig -- GET already returned three
        non-null URLs there, so this path has never actually been
        exercised against a live controller. Raises ProtectAPIError,
        including when the parsed body is not a JSON object. Same
        ``body=""`` redaction as ``get_rtsps_streams`` above, on every
        error this raises.
        """
        path = f"/cameras/{camera_id}/rtsps-stream"
        try:
            raw = self._request("POST", path, body={"qualities": list(qualities)})
        except ProtectAPIError as exc:
            raise _redact_body(exc) from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = _assert_no_secret(f"Invalid JSON response for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body="",
                                   url=f"{self._base_url}{path}") from None
        if not isinstance(parsed, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body="",
                                   url=f"{self._base_url}{path}")
        return parsed

    def _patch_json(self, path: str, body: dict) -> Any:
        """PATCH with a JSON body, returning the parsed JSON response.

        Mirrors `_get_json`'s raw-bytes -> parsed-JSON step (which is
        GET-only), generalized to PATCH -- every PATCH method below
        (patch_sensor/patch_light/patch_chime) needs the same step
        `patch_camera` does inline. Uses the class's own `_request`
        (returns raw bytes; issue #6's addition, GET/PATCH/any verb with an
        optional JSON body).
        """
        raw = self._request("PATCH", path, body=body)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = _assert_no_secret(f"Invalid JSON response for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body=raw[:200].decode(
                "utf-8", errors="replace"), url=f"{self._base_url}{path}") from None

    def _expect_list_of_dicts(self, path: str, body: Any) -> list[dict]:
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected a list of objects",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}", kind="shape")
        return body

    def _expect_dict(self, path: str, body: Any) -> dict:
        if not isinstance(body, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}", kind="shape")
        return body

    # -- Sensors (issue #8) ----------------------------------------------
    # Spec-derived (OpenAPI v6.2.83): the reference rig's /sensors always
    # returned []. UNVERIFIED against real hardware.

    def get_sensors(self) -> list[dict]:
        """GET /sensors. Raises ProtectAPIError, including when the parsed
        body is not a JSON array of objects."""
        path = "/sensors"
        return self._expect_list_of_dicts(path, self._get_json(path))

    def get_sensor(self, sensor_id: str) -> dict:
        """GET /sensors/{id}. Raises ProtectAPIError, including when the
        parsed body is not a JSON object."""
        path = f"/sensors/{sensor_id}"
        return self._expect_dict(path, self._get_json(path))

    def patch_sensor(self, sensor_id: str, body: dict) -> dict:
        """PATCH /sensors/{id}. Returns the updated sensor object. Raises
        ProtectAPIError, including when the parsed body is not a JSON
        object."""
        path = f"/sensors/{sensor_id}"
        return self._expect_dict(path, self._patch_json(path, body))

    # -- Lights (issue #8) ------------------------------------------------
    # Spec-derived (OpenAPI v6.2.83): the reference rig's /lights always
    # returned []. UNVERIFIED against real hardware.

    def get_lights(self) -> list[dict]:
        """GET /lights. Raises ProtectAPIError, including when the parsed
        body is not a JSON array of objects."""
        path = "/lights"
        return self._expect_list_of_dicts(path, self._get_json(path))

    def get_light(self, light_id: str) -> dict:
        """GET /lights/{id}. Raises ProtectAPIError, including when the
        parsed body is not a JSON object."""
        path = f"/lights/{light_id}"
        return self._expect_dict(path, self._get_json(path))

    def patch_light(self, light_id: str, body: dict) -> dict:
        """PATCH /lights/{id}. Returns the updated light object. Raises
        ProtectAPIError, including when the parsed body is not a JSON
        object."""
        path = f"/lights/{light_id}"
        return self._expect_dict(path, self._patch_json(path, body))

    # -- Chimes (issue #8) ------------------------------------------------
    # Spec-derived (OpenAPI v6.2.83): the reference rig's /chimes always
    # returned []. UNVERIFIED against real hardware.

    def get_chimes(self) -> list[dict]:
        """GET /chimes. Raises ProtectAPIError, including when the parsed
        body is not a JSON array of objects."""
        path = "/chimes"
        return self._expect_list_of_dicts(path, self._get_json(path))

    def get_chime(self, chime_id: str) -> dict:
        """GET /chimes/{id}. Raises ProtectAPIError, including when the
        parsed body is not a JSON object."""
        path = f"/chimes/{chime_id}"
        return self._expect_dict(path, self._get_json(path))

    def patch_chime(self, chime_id: str, body: dict) -> dict:
        """PATCH /chimes/{id}. Returns the updated chime object. Raises
        ProtectAPIError, including when the parsed body is not a JSON
        object."""
        path = f"/chimes/{chime_id}"
        return self._expect_dict(path, self._patch_json(path, body))

    # -- NVR (issue #8) -----------------------------------------------------

    def get_nvr(self) -> dict:
        """GET /nvrs.

        Live-verified 2026-08-31 on 7.2.105: the controller returns a
        SINGLE JSON OBJECT, not an array -- matching the OpenAPI spec's
        response schema for this path, which is `nvr`, not `array<nvr>`
        (the plural path name is misleading). Tolerates a one-element list
        defensively in case some deployment differs from both; raises on
        `[]`, a multi-element list, or any other non-dict/non-list shape.

        The live object also carries `armMode`, `type`, `guid`, and `mac`,
        none of which are in the OpenAPI spec -- they are passed through
        unchanged since this method does no schema filtering, only shape
        validation.
        """
        path = "/nvrs"
        body = self._get_json(path)
        if isinstance(body, list):
            if len(body) != 1 or not isinstance(body[0], dict):
                message = _assert_no_secret(
                    f"Unexpected response shape for {path}: expected an object "
                    f"(or a one-element list of one)", self._api_key)
                raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                       url=f"{self._base_url}{path}", kind="shape")
            body = body[0]
        return self._expect_dict(path, body)

    def get_meta_info(self) -> dict:
        """GET /meta/info -> {'applicationVersion': '7.2.105'}.

        Cheapest authenticated call -- a natural connectivity check -- but
        nothing in this codebase calls it yet; it is exposed for a future
        caller to wire up. Raises ProtectAPIError, including when the parsed
        body is not a JSON object.
        """
        path = "/meta/info"
        body = self._get_json(path)
        if not isinstance(body, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}")
        return body
