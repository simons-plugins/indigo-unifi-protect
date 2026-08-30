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

from protect_api import ProtectAPI, ProtectAPIError, _assert_no_secret

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
    mock_urlopen = MagicMock(
        return_value=_FakeResponse(json.dumps({"id": "cam1", "videoMode": "sport"}).encode()))
    monkeypatch.setattr("protect_api.urllib.request.urlopen", mock_urlopen)

    api = make_api(api_key="secret-key")
    result = api.patch_camera("cam1", {"videoMode": "sport"})

    assert result == {"id": "cam1", "videoMode": "sport"}
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


def test_patch_camera_404_unknown_camera_has_no_issues(monkeypatch):
    body = json.dumps({"error": "Entity 'camera' not found", "name": "NOT_FOUND"}).encode()
    monkeypatch.setattr("protect_api.urllib.request.urlopen",
                         MagicMock(side_effect=http_error(404, body=body)))

    api = make_api()
    with pytest.raises(ProtectAPIError) as excinfo:
        api.patch_camera("unknown-cam", {"videoMode": "sport"})

    assert excinfo.value.kind == "not_found"
    assert excinfo.value.issues == []


def test_issues_empty_for_non_json_body():
    """A 400 whose body isn't JSON at all (proxy error page, truncated
    response) must not raise out of `.issues` -- it just has nothing to show.
    """
    exc = ProtectAPIError("HTTP 400 for /cameras/cam1", status=400, body="not json {{{")
    assert exc.issues == []


def test_issues_empty_when_body_has_no_issues_key():
    exc = ProtectAPIError("HTTP 401 for /cameras/cam1", status=401,
                           body=json.dumps({"error": "unauthorized"}))
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
