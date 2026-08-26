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
        kind: Coarse failure category derived from ``status``, so callers
            can react without hardcoding numeric codes:
            "auth" (401/403), "not_found" (404), "rate_limited" (429),
            "bad_request" (400), "server" (5xx), "transport" (status is
            None), "http" (any other non-2xx status).
        retry_after: Seconds to wait before retrying, parsed from the
            response's ``Retry-After`` header when present (most relevant
            for ``kind == "rate_limited"``). ``None`` when absent or
            unparseable.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = "", url: str = "",
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url
        self.retry_after = retry_after
        self.kind = self._classify(status)

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

    def _get(self, path: str, params: Optional[dict[str, str]] = None) -> bytes:
        """Issue a GET request and return the raw response body.

        Raises ProtectAPIError on any non-2xx response or transport failure.
        """
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"X-API-KEY": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout,
                                         context=self._ctx) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            message = _assert_no_secret(f"HTTP {exc.code} for {path}", self._api_key)
            raise ProtectAPIError(message, status=exc.code, body=body, url=url,
                                   retry_after=retry_after) from None
        except urllib.error.URLError as exc:
            message = _assert_no_secret(
                f"Connection failure for {path}: {exc.reason}", self._api_key)
            raise ProtectAPIError(message, status=None, body=str(exc.reason), url=url) from None
        except OSError as exc:
            message = _assert_no_secret(f"Connection failure for {path}: {exc}", self._api_key)
            raise ProtectAPIError(message, status=None, body=str(exc), url=url) from None

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
        """
        path = f"/cameras/{camera_id}/rtsps-stream"
        body = self._get_json(path)
        if not isinstance(body, dict):
            message = _assert_no_secret(
                f"Unexpected response shape for {path}: expected an object",
                self._api_key)
            raise ProtectAPIError(message, status=None, body=str(body)[:200],
                                   url=f"{self._base_url}{path}")
        return body

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
