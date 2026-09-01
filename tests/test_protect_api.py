"""Adversarial test suite for protect_api.ProtectAPI / ProtectAPIError.

Mocks urllib.request.urlopen throughout -- there IS a live UNVR at
192.168.0.10 and this suite must never touch the network.

Per docs/CONTRACT.md and workspace testing convention, the questions asked
are not "does it parse the happy path?" but:

- "can a 200 with the wrong shape escape as a bare TypeError instead of a
  ProtectAPIError?"
- "can a caller tell an auth failure from a rate limit from the network
  being down?"
- "can the API key leak into anything this module raises or logs?"
"""

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from protect_api import ProtectAPI, ProtectAPIError, RTSPS_QUALITIES, _assert_no_secret

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "192.0.2.1"  # TEST-NET-1 (RFC 5737) -- never a real address
FAKE_KEY = "sk-fake-INDIGO-PROTECT-TEST-KEY-9f13c2e4"


def make_api(api_key: str = "test-api-key") -> ProtectAPI:
    return ProtectAPI(HOST, api_key, verify_ssl=False)


class _FakeResponse:
    """Minimal stand-in for the context manager urlopen() returns."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._data


def http_error(code: int, body: bytes = b"", headers: dict[str, str] | None = None,
               url: str = "https://x/y") -> urllib.error.HTTPError:
    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(url, code, f"status {code}", hdrs, io.BytesIO(body))


def url_error(reason: str = "Connection refused") -> urllib.error.URLError:
    return urllib.error.URLError(reason)


# ---------------------------------------------------------------------
# get_cameras -- happy path + shape validation
# ---------------------------------------------------------------------

def test_get_cameras_parses_real_fixture(monkeypatch):
    raw = (FIXTURES / "cameras.json").read_bytes()
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(raw)))

    api = make_api()
    cameras = api.get_cameras()

    assert isinstance(cameras, list)
    assert len(cameras) == 3
    assert all(isinstance(c, dict) for c in cameras)
    ids = {c["id"] for c in cameras}
    assert ids == {
        "69be54f600574703e4000ff4",
        "69bea2c1001d4703e4002686",
        "69ed1b24002f7f03e407c90a",
    }
    names = {c["name"] for c in cameras}
    assert names == {"Side Path", "Patio", "Side Patio"}


def test_get_cameras_dict_body_raises_protect_api_error_not_type_error(monkeypatch):
    """A 200 whose body is a dict (proxy error page, firmware change) must
    raise ProtectAPIError -- NOT escape as a bare TypeError when the caller
    later does c["id"].
    """
    body = json.dumps({"error": "not actually a camera list"}).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))

    api = make_api()
    try:
        api.get_cameras()
    except TypeError:
        pytest.fail("get_cameras() must raise ProtectAPIError, not TypeError, "
                     "on a malformed body")
    except ProtectAPIError:
        pass
    else:
        pytest.fail("get_cameras() should have raised for a dict body")


def test_get_cameras_list_of_non_dicts_also_raises(monkeypatch):
    body = json.dumps(["not", "a", "dict"]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))

    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.get_cameras()


# ---------------------------------------------------------------------
# ProtectAPIError.kind classification
# ---------------------------------------------------------------------

@pytest.mark.parametrize("status,expected_kind", [
    (401, "auth"),
    (403, "auth"),
    (404, "not_found"),
    (429, "rate_limited"),
    (400, "bad_request"),
    (500, "server"),
])
def test_kind_classification_for_http_statuses(monkeypatch, status, expected_kind):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(status, body=b"{}")))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    assert excinfo.value.status == status
    assert excinfo.value.kind == expected_kind


def test_kind_is_transport_for_connection_failure(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=url_error("Connection refused")))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    assert excinfo.value.status is None
    assert excinfo.value.kind == "transport"


# ---------------------------------------------------------------------
# ProtectAPIError.retry_after
# ---------------------------------------------------------------------

def test_retry_after_parsed_from_header_on_429(monkeypatch):
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(side_effect=http_error(429, body=b"{}", headers={"Retry-After": "30"})))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    assert excinfo.value.retry_after == 30.0


def test_retry_after_none_when_header_absent(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(429, body=b"{}")))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    assert excinfo.value.retry_after is None


def test_retry_after_none_for_non_http_errors(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=url_error()))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    assert excinfo.value.retry_after is None


# ---------------------------------------------------------------------
# get_snapshot -- supports_high_quality and the fallback retry
# ---------------------------------------------------------------------

def test_get_snapshot_supports_high_quality_false_never_sends_flag(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b"\xff\xd8\xff\xe0jpeg"))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.get_snapshot("cam1", high_quality=True, supports_high_quality=False)

    assert result == b"\xff\xd8\xff\xe0jpeg"
    assert mock_urlopen.call_count == 1
    requested_url = mock_urlopen.call_args[0][0].full_url
    assert "highQuality" not in requested_url


def test_get_snapshot_high_quality_400_full_hd_retries_once(monkeypatch, caplog):
    first_error = http_error(400, body=b'{"error":"Camera does not support full HD snapshot"}')
    mock_urlopen = MagicMock(side_effect=[first_error, _FakeResponse(b"\xff\xd8\xff\xe0jpeg")])
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.get_snapshot("cam1", high_quality=True)  # supports_high_quality=None

    assert result == b"\xff\xd8\xff\xe0jpeg"
    assert mock_urlopen.call_count == 2
    first_url = mock_urlopen.call_args_list[0][0][0].full_url
    second_url = mock_urlopen.call_args_list[1][0][0].full_url
    assert "highQuality" in first_url
    assert "highQuality" not in second_url


def test_get_snapshot_400_for_other_reason_does_not_retry_and_propagates(monkeypatch):
    only_error = http_error(400, body=b'{"error":"camera offline"}')
    mock_urlopen = MagicMock(side_effect=[only_error])
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_snapshot("cam1", high_quality=True)

    assert excinfo.value.status == 400
    assert mock_urlopen.call_count == 1  # no retry attempted


def test_get_snapshot_supports_high_quality_false_never_retries_on_400(monkeypatch):
    """supports_high_quality=False means highQuality was never sent, so a
    400 here is not the full-HD case and must not trigger the fallback.
    """
    only_error = http_error(400, body=b'{"error":"camera offline"}')
    mock_urlopen = MagicMock(side_effect=[only_error])
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.get_snapshot("cam1", high_quality=True, supports_high_quality=False)

    assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------
# patch_camera -- the plugin's first write path (issue #6)
# ---------------------------------------------------------------------

def test_patch_camera_sends_patch_json_body_content_type_and_key_not_in_url(monkeypatch):
    response_body = {"id": "cam1", "videoMode": "sport", "featureFlags": {"hasHdr": True}}
    mock_urlopen = MagicMock(return_value=_FakeResponse(json.dumps(response_body).encode()))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api(api_key="secret-key")
    result = api.patch_camera("cam1", {"videoMode": "sport"})

    assert result == response_body
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "PATCH"
    assert json.loads(request.data.decode("utf-8")) == {"videoMode": "sport"}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-api-key") == "secret-key"
    assert "secret-key" not in request.full_url


def test_patch_camera_400_ajv_error_is_bad_request_with_issues(monkeypatch):
    ajv_body = json.dumps({
        "error": "Failed to parse 'request-body'",
        "name": "AJV_PARSE_ERROR",
        "entity": "request-body",
        "issues": [{"instancePath": "/videoMode",
                     "message": "must be equal to one of the allowed values",
                     "keyword": "enum"}],
        "body": {"videoMode": "bogus"},
        "isUserError": True,
    }).encode()
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(400, body=ajv_body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("cam1", {"videoMode": "bogus"})

    exc = excinfo.value
    assert exc.kind == "bad_request"
    assert exc.issues == ["/videoMode: must be equal to one of the allowed values"]


def test_patch_camera_404_unknown_camera_keeps_controller_error_as_issue(monkeypatch):
    """A 404 body has no AJV `issues` list, but its `error` string is still
    worth surfacing -- issues falls back to it rather than going empty."""
    body = json.dumps({"error": "Entity 'camera' not found", "name": "NOT_FOUND"}).encode()
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(404, body=body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("unknown-cam", {"videoMode": "sport"})

    assert excinfo.value.kind == "not_found"
    assert excinfo.value.issues == ["Entity 'camera' not found"]


def test_patch_camera_response_missing_id_raises_shape(monkeypatch):
    """A 200 whose body is a dict but doesn't look like the real camera
    object (e.g. a proxy wrapper `{"id": "cam-1"}` with none of the real
    fields) must not be accepted as a cache-worthy camera object -- it
    would blank every hardware state on the caller's side."""
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(return_value=_FakeResponse(json.dumps({"id": "cam1"}).encode())))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("cam1", {"videoMode": "sport"})

    assert excinfo.value.kind == "shape"


def test_patch_camera_response_empty_dict_raises_shape(monkeypatch):
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(return_value=_FakeResponse(b"{}")))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("cam1", {"videoMode": "sport"})

    assert excinfo.value.kind == "shape"


def test_patch_camera_response_id_mismatch_raises_shape(monkeypatch):
    """The response claims to be a different camera than the one PATCHed --
    still not something the caller should cache under camera_id."""
    body = {"id": "some-other-cam", "featureFlags": {"hasHdr": True}}
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(return_value=_FakeResponse(json.dumps(body).encode())))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("cam1", {"videoMode": "sport"})

    assert excinfo.value.kind == "shape"


def test_issues_empty_for_non_json_body():
    """A 400 whose body isn't JSON at all (proxy error page, truncated
    response) must not raise out of `.issues` -- it just has nothing to show.
    """
    exc = ProtectAPIError("HTTP 400 for /cameras/cam1", status=400, body="not json {{{")
    assert exc.issues == []


def test_issues_falls_back_to_error_string_when_no_issues_key():
    exc = ProtectAPIError("HTTP 401 for /cameras/cam1", status=401,
                           body=json.dumps({"error": "unauthorized"}))
    assert exc.issues == ["unauthorized"]


def test_issues_empty_when_body_has_neither_issues_nor_error():
    exc = ProtectAPIError("HTTP 500 for /cameras/cam1", status=500,
                           body=json.dumps({"name": "SERVER_ERROR"}))
    assert exc.issues == []


def test_issues_defaults_missing_instance_path_to_root():
    exc = ProtectAPIError("HTTP 400", status=400, body=json.dumps(
        {"issues": [{"message": "request body must be an object"}]}))
    assert exc.issues == ["/: request body must be an object"]


def test_patch_camera_non_dict_response_raises(monkeypatch):
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(return_value=_FakeResponse(json.dumps(["not", "a", "dict"]).encode())))

    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.patch_camera("cam1", {"videoMode": "sport"})


def test_patch_camera_fake_api_key_never_appears_in_exception(monkeypatch):
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(side_effect=http_error(400, body=b'{"error":"bad request"}')))

    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("cam1", {"videoMode": "sport"})

    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.body
    assert FAKE_KEY not in exc.url
    assert FAKE_KEY not in repr(exc)


# ---------------------------------------------------------------------
# SECURITY -- the API key must never leak into anything this module
# raises or logs. This is a regression guard, not a happy-path check.
# ---------------------------------------------------------------------

def test_assert_no_secret_trips_on_leak():
    """Pin _assert_no_secret's actual enforcement. If this guard is ever
    weakened (e.g. the `if secret and secret in text` check is removed or
    short-circuited), THIS test fails immediately -- it does not depend on
    any particular error path in ProtectAPI to notice the regression.
    """
    with pytest.raises(AssertionError):
        _assert_no_secret(f"oops the key is {FAKE_KEY} right here", FAKE_KEY)


@pytest.mark.parametrize("make_error", [
    lambda: http_error(401, body=b'{"error":"unauthorized"}'),
    lambda: http_error(500, body=b"internal server error"),
    lambda: url_error("Connection refused"),
])
def test_fake_api_key_never_appears_in_raised_exception(monkeypatch, make_error):
    """Construct a client with a distinctive fake API key, drive it through
    several real failure paths, and assert the key appears in NONE of the
    exception's attributes -- str(), .body, .url, repr(). This is the
    integration-level half of the leak guard.
    """
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=make_error()))

    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.body
    assert FAKE_KEY not in exc.url
    assert FAKE_KEY not in repr(exc)


def test_fake_api_key_never_appears_on_malformed_json(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"not json at all {{{")))

    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.body
    assert FAKE_KEY not in exc.url
    assert FAKE_KEY not in repr(exc)


def test_fake_api_key_never_appears_on_shape_validation_failure(monkeypatch):
    body = json.dumps({"not": "a list"}).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))

    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_cameras()

    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.body
    assert FAKE_KEY not in exc.url
    assert FAKE_KEY not in repr(exc)


# ---------------------------------------------------------------------
# get_rtsps_streams (issue #7) -- same body="" redaction on its own
# shape-validation branch as create_rtsps_streams, per the threading/
# leak review: an unexpected body (a proxy error page, a shape change)
# could otherwise echo a URL back through .body.
# ---------------------------------------------------------------------

def test_get_rtsps_streams_parses_real_shape(monkeypatch):
    body = json.dumps({
        "high": "rtsps://192.0.2.1:7441/tok-high?enableSrtp",
        "medium": "rtsps://192.0.2.1:7441/tok-medium?enableSrtp",
        "low": "rtsps://192.0.2.1:7441/tok-low?enableSrtp",
        "package": None,
    }).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))

    api = make_api()
    result = api.get_rtsps_streams("cam1")

    assert result["high"] == "rtsps://192.0.2.1:7441/tok-high?enableSrtp"
    assert result["package"] is None


def test_get_rtsps_streams_shape_error_body_is_never_propagated(monkeypatch):
    """A non-dict body (e.g. a list) fails ProtectAPI's own shape check.
    Unlike every other GET method in this module, this endpoint's shape-
    error body must carry "" -- a malformed response here could plausibly
    contain a URL/token, and that must never be readable off the
    exception.
    """
    leaking_body = json.dumps(
        ["rtsps://192.0.2.1:7441/SECRETTOKEN?enableSrtp"]
    ).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(leaking_body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_rtsps_streams("cam1")

    exc = excinfo.value
    assert exc.body == ""
    assert "SECRETTOKEN" not in str(exc)
    assert "SECRETTOKEN" not in repr(exc)


# ---------------------------------------------------------------------
# create_rtsps_streams (issue #7) -- POST body, and the never-log-the-
# token rule extended to the response BODY of a failed request, not just
# the API key.
# ---------------------------------------------------------------------

def test_create_rtsps_streams_sends_post_with_qualities_body(monkeypatch):
    response_body = json.dumps({
        "high": "rtsps://192.0.2.1:7441/tok-high?enableSrtp",
        "medium": "rtsps://192.0.2.1:7441/tok-medium?enableSrtp",
        "low": "rtsps://192.0.2.1:7441/tok-low?enableSrtp",
        "package": None,
    }).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(response_body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.create_rtsps_streams("cam1", ["high", "medium", "low"])

    assert result["high"] == "rtsps://192.0.2.1:7441/tok-high?enableSrtp"
    assert result["package"] is None
    assert mock_urlopen.call_count == 1
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/cameras/cam1/rtsps-stream")
    assert json.loads(request.data.decode("utf-8")) == {
        "qualities": ["high", "medium", "low"]
    }


def test_create_rtsps_streams_non_dict_response_raises_protect_api_error(monkeypatch):
    body = json.dumps(["not", "a", "dict"]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))

    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.create_rtsps_streams("cam1", ["high"])


def test_create_rtsps_streams_error_body_is_never_propagated(monkeypatch):
    """A URL/token could plausibly appear in this endpoint's error body.
    Unlike every other method in this module, create_rtsps_streams must
    never let that body escape via the exception -- the caller trusts
    ProtectAPIError.body to be safe to log for every OTHER method, but not
    for the one endpoint that can hand back a live-stream URL on failure.
    """
    leaking_body = b'{"error":"rtsps://192.0.2.1:7441/SECRETTOKEN?enableSrtp"}'
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(500, body=leaking_body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.create_rtsps_streams("cam1", ["high"])

    exc = excinfo.value
    assert exc.body == ""
    assert "SECRETTOKEN" not in str(exc)
    assert "SECRETTOKEN" not in repr(exc)


@pytest.mark.parametrize("make_error", [
    lambda: http_error(401, body=b'{"error":"unauthorized"}'),
    lambda: http_error(500, body=b"internal server error"),
    lambda: url_error("Connection refused"),
])
def test_fake_api_key_never_appears_in_create_rtsps_streams_exception(monkeypatch, make_error):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=make_error()))

    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.create_rtsps_streams("cam1", ["high"])

    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.body
    assert FAKE_KEY not in exc.url
    assert FAKE_KEY not in repr(exc)


# ---------------------------------------------------------------------
# Issues #19/#21/#25: PTZ goto/patrol, the alarm-manager webhook, and the
# single-quality RTSPS DELETE.
#
# All five are SPEC-DERIVED, UNVERIFIED against the reference rig (no PTZ
# camera, no Alarm Manager alarms configured). Every one of them declares
# 204 No Content on success, so the questions here are not "does it parse
# the response?" (there IS nothing to parse) but:
#
# - "does a stray non-empty 204 body get mistaken for a parse failure?"
# - "does the slot/quality/webhook-id validation actually run BEFORE any
#   network call, or could a bad value still reach the controller?"
# - "does a user-typed webhook id with '/' or spaces survive as one path
#   segment, or does it get split/mangled?"
# ---------------------------------------------------------------------

def _raise_if_touched():
    """A urlopen mock that fails the test if ever called -- proves a
    validation error was raised BEFORE any network attempt."""
    return MagicMock(side_effect=AssertionError("urlopen must not be called"))


def test_ptz_goto_sends_post_to_slot_path(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.ptz_goto("cam1", 2) is None

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/cameras/cam1/ptz/goto/2")
    assert request.data is None


def test_ptz_goto_stray_response_body_is_discarded(monkeypatch):
    """A 204 is documented, but nothing here should choke if the server
    sends a body anyway -- it must never be parsed."""
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"not json {{{")))

    api = make_api()
    assert api.ptz_goto("cam1", 0) is None


def test_ptz_goto_error_response_raises_with_kind(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(404, body=b'{"error":"not found"}')))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.ptz_goto("cam1", 0)

    assert excinfo.value.kind == "not_found"


@pytest.mark.parametrize("bad_slot", [-1, 10, "2", True, 2.0])
def test_ptz_goto_bad_slot_raises_value_error_before_any_http_call(monkeypatch, bad_slot):
    monkeypatch.setattr("protect_api.urllib.request.urlopen", _raise_if_touched())

    api = make_api()
    with pytest.raises(ValueError):
        api.ptz_goto("cam1", bad_slot)


def test_ptz_goto_accepts_slot_9(monkeypatch):
    """The spec's own OpenAPI `examples` for this endpoint reach 9
    (["-1","0","2","8","9"]), contradicting its prose ("slot 0-4") -- goto
    accepts the wider range since a slot the camera doesn't have is
    refused by the controller, not by this client."""
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.ptz_goto("cam1", 9) is None

    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/cameras/cam1/ptz/goto/9")


def test_ptz_patrol_start_sends_post_to_slot_path(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.ptz_patrol_start("cam1", 4) is None

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/cameras/cam1/ptz/patrol/start/4")


def test_ptz_patrol_start_stray_response_body_is_discarded(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"unexpected junk")))

    api = make_api()
    assert api.ptz_patrol_start("cam1", 0) is None


def test_ptz_patrol_start_error_response_raises_with_kind(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(400, body=b'{"error":"bad slot"}')))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.ptz_patrol_start("cam1", 0)

    assert excinfo.value.kind == "bad_request"


@pytest.mark.parametrize("bad_slot", [-1, 5, "2", True, 2.0])
def test_ptz_patrol_start_bad_slot_raises_value_error_before_any_http_call(
        monkeypatch, bad_slot):
    monkeypatch.setattr("protect_api.urllib.request.urlopen", _raise_if_touched())

    api = make_api()
    with pytest.raises(ValueError):
        api.ptz_patrol_start("cam1", bad_slot)


def test_ptz_patrol_stop_sends_post_no_slot(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.ptz_patrol_stop("cam1") is None

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/cameras/cam1/ptz/patrol/stop")


def test_ptz_patrol_stop_error_response_raises(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(500, body=b"boom")))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.ptz_patrol_stop("cam1")

    assert excinfo.value.kind == "server"


def test_send_alarm_webhook_sends_post_to_encoded_path(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.send_alarm_webhook("AnyRandomString") is None

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/alarm-manager/webhook/AnyRandomString")


def test_send_alarm_webhook_encodes_slash_and_space(monkeypatch):
    """A user-typed trigger id containing '/' or a space must survive as ONE
    path segment, not be split into extra segments or sent unescaped."""
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    api.send_alarm_webhook("front door/alarm 1")

    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/alarm-manager/webhook/front%20door%2Falarm%201")
    assert "front door/alarm 1" not in request.full_url


def test_send_alarm_webhook_stray_response_body_is_discarded(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"junk")))

    api = make_api()
    assert api.send_alarm_webhook("trigger-1") is None


def test_send_alarm_webhook_error_response_raises_with_kind(monkeypatch):
    """400 idRequiredError per the spec -- but any non-2xx must surface as
    ProtectAPIError, not an unhandled HTTPError."""
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(side_effect=http_error(400, body=b'{"error":"idRequiredError"}')))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.send_alarm_webhook("trigger-1")

    assert excinfo.value.kind == "bad_request"


@pytest.mark.parametrize("bad_id", ["", "   ", None, 5])
def test_send_alarm_webhook_bad_id_raises_value_error_before_any_http_call(
        monkeypatch, bad_id):
    monkeypatch.setattr("protect_api.urllib.request.urlopen", _raise_if_touched())

    api = make_api()
    with pytest.raises(ValueError):
        api.send_alarm_webhook(bad_id)


def test_delete_rtsps_stream_sends_delete_with_single_qualities_param(monkeypatch):
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.delete_rtsps_stream("cam1", "high") is None

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "DELETE"
    assert request.full_url == "https://192.0.2.1/proxy/protect/integration/v1" \
        "/cameras/cam1/rtsps-stream?qualities=high"


def test_delete_rtsps_stream_stray_response_body_is_discarded(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"not json {{{")))

    api = make_api()
    assert api.delete_rtsps_stream("cam1", "package") is None


def test_delete_rtsps_stream_error_response_body_is_never_propagated(monkeypatch):
    """Same redaction as get/create_rtsps_streams -- this endpoint family
    can echo a stream URL/token back in an error body."""
    leaking_body = b'{"error":"rtsps://192.0.2.1:7441/SECRETTOKEN?enableSrtp"}'
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(500, body=leaking_body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.delete_rtsps_stream("cam1", "high")

    exc = excinfo.value
    assert exc.body == ""
    assert exc.kind == "server"
    assert "SECRETTOKEN" not in str(exc)
    assert "SECRETTOKEN" not in repr(exc)


def test_delete_rtsps_stream_not_found_kind_preserved_after_redaction(monkeypatch):
    monkeypatch.setattr(
        "protect_api.urllib.request.urlopen",
        MagicMock(side_effect=http_error(404, body=b'{"error":"Entity not found"}')))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.delete_rtsps_stream("cam1", "low")

    assert excinfo.value.kind == "not_found"
    assert excinfo.value.body == ""


@pytest.mark.parametrize("bad_quality", ["High", "all", "", None, 1])
def test_delete_rtsps_stream_bad_quality_raises_value_error_before_any_http_call(
        monkeypatch, bad_quality):
    monkeypatch.setattr("protect_api.urllib.request.urlopen", _raise_if_touched())

    api = make_api()
    with pytest.raises(ValueError):
        api.delete_rtsps_stream("cam1", bad_quality)


@pytest.mark.parametrize("quality", RTSPS_QUALITIES)
def test_delete_rtsps_stream_every_rtsps_quality_reaches_urlopen(monkeypatch, quality):
    """Pins RTSPS_QUALITIES as the single source of truth for what
    delete_rtsps_stream accepts -- every value in the tuple must pass its
    own validation, not just the four hardcoded literals a hand-written
    test would happen to pick."""
    mock_urlopen = MagicMock(return_value=_FakeResponse(b""))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    assert api.delete_rtsps_stream("cam1", quality) is None
    assert mock_urlopen.called


# ---------------------------------------------------------------------
# Issue #8: sensors/lights/chimes/nvr via the new _request helper.
#
# Spec-derived (OpenAPI v6.2.83) -- the reference rig's /sensors, /lights,
# /chimes all return []; /nvrs is the one endpoint with real live data.
# ---------------------------------------------------------------------

def test_get_sensors_sends_get_and_parses_list(monkeypatch):
    body = json.dumps([{"id": "s1", "modelKey": "sensor"}]).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.get_sensors()

    assert result == [{"id": "s1", "modelKey": "sensor"}]
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/sensors")


def test_get_sensors_dict_body_raises(monkeypatch):
    body = json.dumps({"not": "a list"}).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.get_sensors()


def test_get_sensor_sends_get_with_id_in_path(monkeypatch):
    body = json.dumps({"id": "s1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.get_sensor("s1")

    assert result == {"id": "s1"}
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/sensors/s1")


def test_patch_sensor_sends_patch_with_body(monkeypatch):
    body = json.dumps({"id": "s1", "motionSettings": {"isEnabled": False}}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.patch_sensor("s1", {"motionSettings": {"isEnabled": False}})

    assert result["motionSettings"]["isEnabled"] is False
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "PATCH"
    assert request.full_url.endswith("/sensors/s1")
    assert json.loads(request.data.decode("utf-8")) == {"motionSettings": {"isEnabled": False}}


def test_patch_sensor_non_dict_response_raises(monkeypatch):
    body = json.dumps(["not", "a", "dict"]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.patch_sensor("s1", {})


def test_get_lights_sends_get_and_parses_list(monkeypatch):
    body = json.dumps([{"id": "l1", "modelKey": "light"}]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    assert api.get_lights() == [{"id": "l1", "modelKey": "light"}]


def test_get_light_sends_get_with_id_in_path(monkeypatch):
    body = json.dumps({"id": "l1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)
    api = make_api()
    api.get_light("l1")
    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/lights/l1")


def test_patch_light_sends_patch_with_body(monkeypatch):
    body = json.dumps({"id": "l1", "isLightForceEnabled": True}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.patch_light("l1", {"isLightForceEnabled": True})

    assert result["isLightForceEnabled"] is True
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "PATCH"
    assert json.loads(request.data.decode("utf-8")) == {"isLightForceEnabled": True}


def test_get_chimes_sends_get_and_parses_list(monkeypatch):
    body = json.dumps([{"id": "c1", "modelKey": "chime"}]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    assert api.get_chimes() == [{"id": "c1", "modelKey": "chime"}]


def test_get_chime_sends_get_with_id_in_path(monkeypatch):
    body = json.dumps({"id": "c1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)
    api = make_api()
    api.get_chime("c1")
    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/chimes/c1")


def test_patch_chime_sends_patch_with_ring_settings_body(monkeypatch):
    body = json.dumps({"id": "c1", "ringSettings": []}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    ring_settings = [{"cameraId": "cam1", "repeatTimes": 1, "ringtoneId": "r1", "volume": 50}]
    api.patch_chime("c1", {"ringSettings": ring_settings})

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "PATCH"
    assert json.loads(request.data.decode("utf-8")) == {"ringSettings": ring_settings}


# -- NVR: dict, one-element list, and every rejected shape ---------------

def test_get_nvr_accepts_a_bare_dict(monkeypatch):
    """The live-observed shape: a single JSON object, not an array."""
    body = json.dumps({
        "id": "nvr1", "modelKey": "nvr", "name": "UNVR",
        "armMode": {"status": "disabled"},
    }).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.get_nvr()

    assert result["id"] == "nvr1"
    assert result["armMode"]["status"] == "disabled"
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/nvrs")


def test_get_nvr_accepts_a_one_element_list(monkeypatch):
    """Defensive tolerance in case some deployment wraps the object in an
    array, even though the reference rig and the OpenAPI spec both say the
    response is a bare object."""
    body = json.dumps([{"id": "nvr1", "modelKey": "nvr"}]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    result = api.get_nvr()
    assert result["id"] == "nvr1"


@pytest.mark.parametrize("raw_body", [
    b"[]",
    b'[{"id":"a"},{"id":"b"}]',
    b'"just a string"',
    b"42",
    b"null",
])
def test_get_nvr_rejects_every_other_shape(monkeypatch, raw_body):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(raw_body)))
    api = make_api()
    with pytest.raises(ProtectAPIError):
        api.get_nvr()


# -- Viewers (issue #22) / Liveviews (issue #23, read-only) --------------

def test_get_viewers_sends_get_and_parses_list(monkeypatch):
    body = json.dumps([{"id": "v1", "modelKey": "viewer"}]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    assert api.get_viewers() == [{"id": "v1", "modelKey": "viewer"}]


def test_get_viewer_sends_get_with_id_in_path(monkeypatch):
    body = json.dumps({"id": "v1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)
    api = make_api()
    api.get_viewer("v1")
    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/viewers/v1")


def test_patch_viewer_sends_patch_with_liveview_body(monkeypatch):
    body = json.dumps({"id": "v1", "liveview": "lv-1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api()
    result = api.patch_viewer("v1", {"liveview": "lv-1"})

    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "PATCH"
    assert json.loads(request.data.decode("utf-8")) == {"liveview": "lv-1"}
    assert result == {"id": "v1", "liveview": "lv-1"}


def test_get_viewers_rejects_a_bare_object():
    """Same shape guard every list endpoint gets -- a dict instead of a
    list must not silently become an empty/[dict]-shaped iteration
    surprise for a caller doing `for v in get_viewers()`."""
    api = make_api()
    with pytest.raises(ProtectAPIError):
        api._expect_list_of_dicts("/viewers", {"id": "v1"})


def test_get_liveviews_sends_get_and_parses_list(monkeypatch):
    body = json.dumps([{"id": "lv-1", "modelKey": "liveview", "isDefault": True}]).encode("utf-8")
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(body)))
    api = make_api()
    assert api.get_liveviews() == [{"id": "lv-1", "modelKey": "liveview", "isDefault": True}]


def test_get_liveview_sends_get_with_id_in_path(monkeypatch):
    body = json.dumps({"id": "lv-1"}).encode("utf-8")
    mock_urlopen = MagicMock(return_value=_FakeResponse(body))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)
    api = make_api()
    api.get_liveview("lv-1")
    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/liveviews/lv-1")


def test_protect_api_has_no_liveview_write_methods():
    """Issue #23 scopes this plugin to READ-ONLY live views -- the spec
    documents POST/PATCH for liveviews, but this client must not offer a
    way to call them (a future caller reaching for one would otherwise
    silently invent a wire contract nobody has verified)."""
    api = make_api()
    assert not hasattr(api, "create_liveview")
    assert not hasattr(api, "patch_liveview")


def test_request_helper_returns_raw_bytes(monkeypatch):
    """_request (issue #6) returns the raw response body -- JSON parsing is
    each caller's own job (_get_json/_patch_json), matching patch_camera's
    existing inline pattern."""
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b'{"id":"s1"}')))
    api = make_api()
    assert api._request("PATCH", "/sensors/s1", body={"name": "x"}) == b'{"id":"s1"}'


def test_patch_json_empty_body_raises_not_none(monkeypatch):
    """A 2xx with no body (e.g. a 204) cannot be parsed as JSON -- _patch_json
    (and _get_json) raise ProtectAPIError rather than silently returning
    None, matching patch_camera's own empty/malformed-body handling."""
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(return_value=_FakeResponse(b"")))
    api = make_api()
    with pytest.raises(ProtectAPIError):
        api._patch_json("/sensors/s1", {"name": "x"})


def test_fake_api_key_never_appears_in_request_helper_exception(monkeypatch):
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(500, body=b"server error")))
    api = make_api(api_key=FAKE_KEY)
    with pytest.raises(ProtectAPIError) as excinfo:
        api.get_sensors()
    exc = excinfo.value
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in exc.url
