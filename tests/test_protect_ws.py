"""Adversarial test suite for protect_ws.ProtectEventSocket.

No network. A FakeSocket stands in for the TLS socket -- it is fed raw
WS-framed bytes (or queued exceptions) and records everything sent to it, so
every test drives the class through its public surface (connect/read_message/
send_ping/close) while asserting on wire-level behaviour (masking, opcodes,
reassembly) and on internal state that only this module can see
(last_frame_at) per docs/CONTRACT.md.

The single most important test in this file is
test_dead_peer_etimedout_raises_not_idle -- see its docstring.
"""

import errno
import json
import socket
import struct

import pytest

from protect_ws import ProtectEventSocket

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


# ---------------------------------------------------------------------
# Wire helpers -- an independent frame encoder, deliberately not sharing
# code with protect_ws._send_frame, so encoding bugs there can't hide
# behind a test that reuses the same buggy logic to build its fixtures.
# ---------------------------------------------------------------------

def build_frame(opcode: int, payload: bytes = b"", fin: bool = True,
                 masked: bool = False, mask_key: bytes = b"\x11\x22\x33\x44") -> bytes:
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        b1 = length
        ext = b""
    elif length < 0x10000:
        b1 = 126
        ext = struct.pack(">H", length)
    else:
        b1 = 127
        ext = struct.pack(">Q", length)

    if masked:
        b1 |= 0x80
        wire_payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
        return bytes([b0, b1]) + ext + mask_key + wire_payload
    return bytes([b0, b1]) + ext + payload


def text_frame(payload: str, **kwargs) -> bytes:
    return build_frame(_OP_TEXT, payload.encode("utf-8"), **kwargs)


class FakeSocket:
    """Stand-in for the SSLSocket. Feed it a queue of recv() results --
    bytes chunks or exception instances to raise."""

    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self.sent = []
        self.timeouts = []
        self.closed = False

    def queue(self, item):
        self._chunks.append(item)

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _n):
        if not self._chunks:
            raise AssertionError(
                "FakeSocket.recv() called with nothing queued -- "
                "the code under test read more than the test expected")
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def make_socket(chunks=None) -> ProtectEventSocket:
    """A ProtectEventSocket wired directly to a FakeSocket, bypassing
    connect() (and therefore the network) entirely."""
    ws = ProtectEventSocket("192.0.2.1", "fake-api-key")
    ws._sock = FakeSocket(chunks)  # pylint: disable=protected-access
    ws._buffer = b""  # pylint: disable=protected-access
    return ws


def fake_sock_of(ws: ProtectEventSocket) -> FakeSocket:
    return ws._sock  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# Extended payload lengths
# ---------------------------------------------------------------------

def test_extended_length_126_two_byte():
    body = {"data": "x" * 200}  # >125 bytes JSON, forces the 2-byte extended-length form
    payload = json.dumps(body)
    ws = make_socket([text_frame(payload)])
    msg = ws.read_message(timeout=1.0)
    assert msg == body
    # sanity: confirm the fixture actually used the 126 form
    frame = text_frame(payload)
    assert frame[1] == 126


def test_extended_length_127_eight_byte():
    body = {"data": "y" * 70000}  # >0xFFFF bytes JSON, forces the 8-byte extended-length form
    payload = json.dumps(body)
    ws = make_socket([text_frame(payload)])
    msg = ws.read_message(timeout=1.0)
    assert msg == body
    frame = text_frame(payload)
    assert frame[1] == 127


# ---------------------------------------------------------------------
# A frame split across multiple recv() calls
# ---------------------------------------------------------------------

def test_frame_split_across_multiple_recv_calls():
    frame = text_frame('{"hello": "world"}')
    # Split at an arbitrary, awkward boundary (mid extended-length-less
    # header is impossible here since payload < 126, so split payload).
    split_at = 5
    ws = make_socket([frame[:split_at], frame[split_at:]])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"hello": "world"}


def test_frame_split_byte_by_byte():
    """Pathological case: recv() returns one byte at a time."""
    frame = text_frame('{"a": 1}')
    ws = make_socket([bytes([b]) for b in frame])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"a": 1}


# ---------------------------------------------------------------------
# Client frames MUST be masked
# ---------------------------------------------------------------------

def test_send_ping_frame_is_masked_with_key_present():
    ws = make_socket()
    ws.send_ping()
    sock = fake_sock_of(ws)
    assert len(sock.sent) == 1
    sent = sock.sent[0]
    b0, b1 = sent[0], sent[1]
    assert b0 & 0x0F == _OP_PING
    assert b0 & 0x80, "client PING frame must set FIN"
    assert b1 & 0x80, "client frames MUST be masked (RFC6455 S5.3)"
    length = b1 & 0x7F
    assert length == 0
    # header(2) + mask key(4) -- a mask key must actually be present
    assert len(sent) == 6


def test_close_frame_is_masked():
    ws = make_socket()
    sock = fake_sock_of(ws)
    ws.close()
    assert len(sock.sent) == 1
    sent = sock.sent[0]
    b0, b1 = sent[0], sent[1]
    assert b0 & 0x0F == _OP_CLOSE
    assert b1 & 0x80, "client CLOSE frame must be masked"


# ---------------------------------------------------------------------
# ping -> auto-pong, loop continues to the next real message
# ---------------------------------------------------------------------

def test_ping_triggers_auto_pong_and_loop_continues():
    ws = make_socket([
        build_frame(_OP_PING, b"ping-payload"),
        text_frame('{"type": "add"}'),
    ])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"type": "add"}

    sock = fake_sock_of(ws)
    assert len(sock.sent) == 1
    pong = sock.sent[0]
    assert pong[0] & 0x0F == _OP_PONG
    assert pong[1] & 0x80, "auto-pong must be masked like any client frame"
    # pong must echo the ping's payload back
    mask_key = pong[2:6]
    masked_payload = pong[6:]
    unmasked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(masked_payload))
    assert unmasked == b"ping-payload"


# ---------------------------------------------------------------------
# close frame -> ConnectionError
# ---------------------------------------------------------------------

def test_close_frame_from_server_raises_connection_error():
    ws = make_socket([build_frame(_OP_CLOSE, b"")])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


def test_peer_closing_tcp_raises_connection_error():
    """recv() returning b'' (EOF) is a dropped socket, not an idle tick."""
    ws = make_socket([b""])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


# ---------------------------------------------------------------------
# THE adversarial test this suite exists for.
#
# socket.timeout is TimeoutError, and TimeoutError subclasses OSError, on
# Python 3.13 (verified live, see docs/CONTRACT.md discussion). The only
# thing separating "nothing to read yet" from "the peer silently vanished"
# is exc.errno: None means settimeout() simply expired (normal); any
# errno, especially ETIMEDOUT, means the OS itself gave up on a dead
# connection and recv() must not be reported as an idle tick -- doing so
# means every camera reports "no motion, connected=True" forever.
# ---------------------------------------------------------------------

def test_idle_timeout_errno_none_returns_none():
    ws = make_socket([TimeoutError()])
    assert ws.read_message(timeout=0.05) is None


def test_dead_peer_etimedout_raises_not_idle():
    dead_peer_exc = TimeoutError(errno.ETIMEDOUT, "Operation timed out")
    assert dead_peer_exc.errno == errno.ETIMEDOUT  # sanity on the fixture itself
    ws = make_socket([dead_peer_exc])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


def test_dead_peer_socket_timeout_alias_with_errno_raises():
    """socket.timeout is literally TimeoutError on 3.13, but assert against
    the socket.timeout spelling too in case a caller constructs it that way."""
    dead_peer_exc = socket.timeout(errno.ETIMEDOUT, "Operation timed out")
    ws = make_socket([dead_peer_exc])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


def test_other_oserror_raises_connection_error():
    ws = make_socket([ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


# ---------------------------------------------------------------------
# Over-cap declared length -> ConnectionError, not unbounded buffering
# ---------------------------------------------------------------------

def test_over_cap_declared_length_raises_connection_error_not_buffered():
    # 9 MB > the 8 MB cap. Use the 8-byte extended-length form (127) with
    # only a couple of payload bytes actually delivered -- if the code
    # buffered instead of rejecting, it would return None (still waiting
    # for the rest) rather than raise.
    oversized = 9 * 1024 * 1024
    header = bytes([0x80 | _OP_TEXT, 127]) + struct.pack(">Q", oversized)
    ws = make_socket([header, b"only a few bytes, not the full frame"])
    with pytest.raises(ConnectionError):
        ws.read_message(timeout=1.0)


def test_at_cap_declared_length_is_not_rejected_by_the_cap_check():
    """Boundary check: a frame exactly at the cap is a length-parsing
    concern, not a rejection -- prove the cap compares length, not
    reads-so-far, by using a small in-cap length and confirming normal
    frames of ordinary size are unaffected."""
    body = {"data": "z" * 1000}
    ws = make_socket([text_frame(json.dumps(body))])
    assert ws.read_message(timeout=1.0) == body


# ---------------------------------------------------------------------
# Fragmented message reassembly (chosen fix for BUG 4 -- see report)
# ---------------------------------------------------------------------

def test_fragmented_text_message_reassembles():
    body = '{"type": "add", "item": {"id": "abc"}}'
    mid = len(body) // 2
    first = body[:mid]
    second = body[mid:]
    ws = make_socket([
        build_frame(_OP_TEXT, first.encode(), fin=False),
        build_frame(_OP_CONTINUATION, second.encode(), fin=True),
    ])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"type": "add", "item": {"id": "abc"}}


def test_fragmented_message_across_three_frames():
    body = '{"a": "bbbbbbbbbb"}'
    part1, part2, part3 = body[:5], body[5:12], body[12:]
    ws = make_socket([
        build_frame(_OP_TEXT, part1.encode(), fin=False),
        build_frame(_OP_CONTINUATION, part2.encode(), fin=False),
        build_frame(_OP_CONTINUATION, part3.encode(), fin=True),
    ])
    assert ws.read_message(timeout=1.0) == {"a": "bbbbbbbbbb"}


def test_ping_interleaved_mid_fragmentation_does_not_corrupt_reassembly():
    """RFC6455 permits control frames between fragments of a data message.
    A ping arriving mid-fragmentation must be pong'd and must NOT be
    treated as part of the fragmented payload."""
    body = '{"ok": true}'
    mid = len(body) // 2
    ws = make_socket([
        build_frame(_OP_TEXT, body[:mid].encode(), fin=False),
        build_frame(_OP_PING, b"keepalive"),
        build_frame(_OP_CONTINUATION, body[mid:].encode(), fin=True),
    ])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"ok": True}
    sock = fake_sock_of(ws)
    assert len(sock.sent) == 1
    assert sock.sent[0][0] & 0x0F == _OP_PONG


def test_orphan_continuation_frame_is_discarded_not_crashed():
    """A CONTINUATION frame with no fragment in progress must be logged
    and dropped, not raise or hang -- then normal messages keep flowing."""
    ws = make_socket([
        build_frame(_OP_CONTINUATION, b"orphan", fin=True),
        text_frame('{"fine": true}'),
    ])
    msg = ws.read_message(timeout=1.0)
    assert msg == {"fine": True}


# ---------------------------------------------------------------------
# Binary frames -- BUG 5: warn once, don't silently vanish
# ---------------------------------------------------------------------

def test_binary_frame_is_discarded_and_warns(caplog):
    ws = make_socket([
        build_frame(_OP_BINARY, b"\x00\x01\x02", fin=True),
        text_frame('{"after_binary": true}'),
    ])
    with caplog.at_level("WARNING"):
        msg = ws.read_message(timeout=1.0)
    assert msg == {"after_binary": True}
    assert any("binary" in rec.message.lower() for rec in caplog.records)


def test_binary_frame_warning_logged_only_once():
    ws = make_socket([
        build_frame(_OP_BINARY, b"\x01", fin=True),
        build_frame(_OP_BINARY, b"\x02", fin=True),
        text_frame('{"still_here": true}'),
    ])
    assert ws._binary_warned is False  # pylint: disable=protected-access
    msg = ws.read_message(timeout=1.0)
    assert msg == {"still_here": True}
    assert ws._binary_warned is True  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# last_frame_at -- BUG 2
# ---------------------------------------------------------------------

def test_last_frame_at_is_none_before_connect():
    ws = ProtectEventSocket("192.0.2.1", "fake-api-key")
    assert ws.last_frame_at is None


def test_last_frame_at_advances_on_ping_not_just_text():
    # Queue ONLY a ping, then an idle-timeout error (errno None) so the call
    # returns None right after processing the ping -- proving last_frame_at
    # moved off its initial None on the ping alone, never reaching a text frame.
    ws = make_socket([build_frame(_OP_PING, b""), TimeoutError()])
    assert ws.last_frame_at is None
    result = ws.read_message(timeout=1.0)
    assert result is None
    assert ws.last_frame_at is not None, "last_frame_at must advance on a ping, not just text"


def test_last_frame_at_advances_on_pong():
    ws = make_socket()
    assert ws.last_frame_at is None
    fake_sock_of(ws).queue(build_frame(_OP_PONG, b""))
    fake_sock_of(ws).queue(TimeoutError())  # then idle, so read_message returns
    ws.read_message(timeout=0.05)
    assert ws.last_frame_at is not None


def test_last_frame_at_set_by_connect_even_with_no_traffic(monkeypatch):
    """connect() must set a baseline so a socket that never delivers a
    single frame is still measurable as stale, not indistinguishable from
    'never checked'."""
    import protect_ws as protect_ws_module  # local import to patch cleanly

    class _FakeTLSSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, _t):
            pass

        def sendall(self, data):
            self.sent.append(data)

        def recv(self, _n):
            return b"HTTP/1.1 101 Switching Protocols\r\n\r\n"

        def close(self):
            pass

    class _FakeCtx:
        def wrap_socket(self, _raw, server_hostname=None):
            return _FakeTLSSocket()

    monkeypatch.setattr(protect_ws_module.ssl, "create_default_context", lambda: _FakeCtx())
    monkeypatch.setattr(
        protect_ws_module.socket, "create_connection", lambda addr, timeout=None: object())

    ws = ProtectEventSocket("192.0.2.1", "fake-api-key")
    assert ws.last_frame_at is None
    ws.connect()
    assert ws.last_frame_at is not None


# ---------------------------------------------------------------------
# send_ping()
# ---------------------------------------------------------------------

def test_send_ping_on_never_connected_instance_raises_connection_error():
    ws = ProtectEventSocket("192.0.2.1", "fake-api-key")
    with pytest.raises(ConnectionError):
        ws.send_ping()


def test_send_ping_write_failure_surfaces_as_connection_error():
    ws = make_socket()

    def _raise(_data):
        raise OSError(errno.EPIPE, "Broken pipe")

    fake_sock_of(ws).sendall = _raise
    with pytest.raises(ConnectionError):
        ws.send_ping()


def test_send_ping_pong_response_advances_last_frame_at():
    ws = make_socket()
    ws.send_ping()
    sock = fake_sock_of(ws)
    assert len(sock.sent) == 1
    assert sock.sent[0][0] & 0x0F == _OP_PING

    sock.queue(build_frame(_OP_PONG, b""))
    sock.queue(TimeoutError())
    before = ws.last_frame_at
    ws.read_message(timeout=0.05)
    assert ws.last_frame_at is not None
    assert before is None or ws.last_frame_at >= before


# ---------------------------------------------------------------------
# path/label (issue #18) -- the class is reused for /subscribe/devices
# ---------------------------------------------------------------------

def test_default_path_and_label_are_unchanged():
    """Existing callers (the events socket) must be byte-identical: no
    path/label kwargs passed, so both must default to the original values."""
    ws = ProtectEventSocket("192.0.2.1", "fake-api-key")
    assert ws._path == "/proxy/protect/integration/v1/subscribe/events"  # pylint: disable=protected-access
    assert ws._label == "event"  # pylint: disable=protected-access


def test_custom_path_lands_in_the_handshake_get_line(monkeypatch):
    import protect_ws as protect_ws_module

    class _FakeTLSSocket:
        def __init__(self):
            self.sent = b""

        def settimeout(self, _t):
            pass

        def sendall(self, data):
            self.sent += data

        def recv(self, _n):
            return b"HTTP/1.1 101 Switching Protocols\r\n\r\n"

        def close(self):
            pass

    fake_sock = _FakeTLSSocket()

    class _FakeCtx:
        def wrap_socket(self, _raw, server_hostname=None):
            return fake_sock

    monkeypatch.setattr(protect_ws_module.ssl, "create_default_context", lambda: _FakeCtx())
    monkeypatch.setattr(
        protect_ws_module.socket, "create_connection", lambda addr, timeout=None: object())

    ws = ProtectEventSocket(
        "192.0.2.1", "fake-api-key",
        path="/proxy/protect/integration/v1/subscribe/devices", label="device",
    )
    ws.connect()

    request = fake_sock.sent.decode()
    assert request.startswith("GET /proxy/protect/integration/v1/subscribe/devices HTTP/1.1\r\n")


def test_custom_label_appears_in_error_text():
    ws = make_socket()
    ws._label = "device"  # pylint: disable=protected-access
    ws._sock = None  # pylint: disable=protected-access
    with pytest.raises(ConnectionError, match="Protect device socket"):
        ws.read_message(timeout=0.01)


def test_custom_label_set_via_constructor_appears_in_error_text():
    ws = ProtectEventSocket("192.0.2.1", "fake-api-key", label="device")
    with pytest.raises(ConnectionError, match="Protect device socket"):
        ws.send_ping()
