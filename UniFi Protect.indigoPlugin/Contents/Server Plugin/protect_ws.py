"""Stdlib-only WebSocket client for UniFi Protect's plain-JSON sockets.

Speaks plain RFC 6455 (text opcode 0x1, no compression) to
``/proxy/protect/integration/v1/subscribe/events`` by default, authenticated
with the same ``X-API-KEY`` header used for the REST API. Handshake and
frame-reader lifted from ``docs/ws_probe.py``, which was proven live against
this server.

Issue #18 reuses this same class for the sibling
``/subscribe/devices`` socket via the ``path``/``label`` constructor kwargs
(both default to the original events-socket values, so every pre-existing
caller is unaffected) -- the two sockets share an identical handshake and
frame format, just a different path and a different label in log/error text.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import time
from typing import Optional

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_HANDSHAKE_PATH = "/proxy/protect/integration/v1/subscribe/events"

# Reject any declared frame payload length above this. A corrupt/desynced
# 8-byte length field must never be trusted to grow _buffer without bound --
# that turns a real transport error into a silent, permanent "idle" read.
_MAX_FRAME_PAYLOAD = 8 * 1024 * 1024


def _assert_no_secret(text: str, secret: str) -> str:
    """Defensive guard: fail loudly if the API key ever leaks into text."""
    if secret and secret in text:
        raise AssertionError("API key must never appear in logged/exception text")
    return text


class ProtectEventSocket:
    """Blocking, stdlib-only WebSocket client for /subscribe/events (the
    default) or, via the `path`/`label` kwargs, /subscribe/devices
    (issue #18) -- both sockets share an identical handshake and frame
    format.

    Not thread-safe. One instance is driven by one thread.
    """

    def __init__(self, host: str, api_key: str, verify_ssl: bool = False,
                 logger: Optional[logging.Logger] = None,
                 path: str = _HANDSHAKE_PATH, label: str = "event") -> None:
        self._host = host
        self._api_key = api_key
        self._verify_ssl = verify_ssl
        self._logger = logger or logging.getLogger(__name__)
        self._path = path
        self._label = label
        self._sock: Optional[ssl.SSLSocket] = None
        self._buffer = b""
        self._last_frame_at: Optional[float] = None
        self._frag_opcode: Optional[int] = None
        self._frag_payload = bytearray()
        self._binary_warned = False

    @property
    def last_frame_at(self) -> Optional[float]:
        """Monotonic timestamp of the last WS frame received, of ANY
        opcode (including ping/pong/continuation) -- not just decoded JSON
        messages. Also set by connect(), so a socket that never delivers a
        single frame is still measurable as stale. None before connect().

        The caller (plugin.py) uses this as a staleness watchdog: a dropped
        socket can go quiet with no errno at all, and read_message() alone
        cannot detect that.
        """
        return self._last_frame_at

    def connect(self) -> None:
        """Open TCP+TLS, perform the RFC6455 handshake with the X-API-KEY
        header, and validate the 101 response. Raises ConnectionError on
        anything other than '101'.
        """
        ctx = ssl.create_default_context()
        if not self._verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            raw = socket.create_connection((self._host, 443), timeout=15)
            sock = ctx.wrap_socket(raw, server_hostname=self._host)
        except OSError as exc:
            raise ConnectionError(f"Protect {self._label} socket connect failed: {exc}") from None

        nonce = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"X-API-KEY: {self._api_key}\r\n\r\n"
        )
        sock.settimeout(15)
        try:
            sock.sendall(request.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError(f"Protect {self._label} socket closed during handshake")
                buf += chunk
        except (OSError, socket.timeout) as exc:
            sock.close()
            if isinstance(exc, ConnectionError):
                raise
            raise ConnectionError(f"Protect {self._label} socket handshake failed: {exc}") from None

        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            sock.close()
            message = _assert_no_secret(
                f"Protect {self._label} socket handshake rejected: "
                f"{status_line.decode(errors='replace')}", self._api_key)
            raise ConnectionError(message)

        self._sock = sock
        self._buffer = rest
        self._frag_opcode = None
        self._frag_payload = bytearray()
        self._last_frame_at = time.monotonic()

    def send_ping(self) -> None:
        """Send a client PING frame.

        The server's PONG reply updates last_frame_at, which is what lets a
        caller distinguish a quiet-but-healthy socket (the server sends no
        idle keepalive of its own -- long gaps with no motion are normal)
        from a dead one. Raises ConnectionError if the socket is not
        connected or the send fails -- a write failure is one of the few
        ways a half-open socket reveals itself early, so it must not be
        swallowed.
        """
        self._send_frame(_OP_PING)

    def read_message(self, timeout: float = 1.0) -> Optional[dict]:
        """Return the next decoded JSON message, or None if the timeout
        elapsed with no complete frame (this is normal and not an error --
        it is how the caller stays responsive to shutdown).

        Handles internally, returning None for each: ping (must reply pong),
        pong, and continuation frames (reassembled -- see _take_frame/
        _decode_data_frame). Raises ConnectionError on a close frame or a
        dropped socket -- including a peer that vanished silently (recv()
        raising a timeout error WITH an errno, e.g. ETIMEDOUT, is a dead
        connection, not an idle tick; only a bare settimeout() expiry, with
        no errno, is normal and returns None).
        """
        if self._sock is None:
            raise ConnectionError(f"Protect {self._label} socket is not connected")

        deadline = time.monotonic() + timeout
        while True:
            frame = self._take_frame()
            if frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    self._sock.settimeout(remaining)
                    chunk = self._sock.recv(65536)
                except (socket.timeout, TimeoutError) as exc:
                    if exc.errno is not None:
                        # A real errno (e.g. ETIMEDOUT) means the peer is
                        # gone -- a NAT/firewall flow that expired silently,
                        # no FIN/RST. Only a bare settimeout() expiry (no
                        # errno set) is a normal idle tick.
                        raise ConnectionError(
                            f"Protect {self._label} socket read failed: {exc}") from None
                    return None
                except OSError as exc:
                    raise ConnectionError(f"Protect {self._label} socket read failed: {exc}") from None
                if not chunk:
                    raise ConnectionError(f"Protect {self._label} socket closed by peer")
                self._buffer += chunk
                continue

            fin, opcode, payload = frame
            self._last_frame_at = time.monotonic()

            if opcode == _OP_CLOSE:
                raise ConnectionError(f"Protect {self._label} socket received a close frame")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue

            if opcode in (_OP_TEXT, _OP_BINARY):
                if not fin:
                    # First frame of a fragmented message -- buffer until a
                    # CONTINUATION frame with FIN set completes it.
                    self._frag_opcode = opcode
                    self._frag_payload = bytearray(payload)
                    continue
                result = self._decode_data_frame(opcode, payload)
                if result is not None:
                    return result
                continue

            if opcode == _OP_CONTINUATION:
                if self._frag_opcode is None:
                    self._logger.warning(
                        "Discarding orphan WS continuation frame "
                        "(no fragmented message in progress)")
                    continue
                self._frag_payload.extend(payload)
                if not fin:
                    continue
                complete_opcode = self._frag_opcode
                complete_payload = bytes(self._frag_payload)
                self._frag_opcode = None
                self._frag_payload = bytearray()
                result = self._decode_data_frame(complete_opcode, complete_payload)
                if result is not None:
                    return result
                continue

            # Any other opcode carries nothing usable here.
            continue

    def _decode_data_frame(self, opcode: int, payload: bytes) -> Optional[dict]:
        """Decode one complete (already-reassembled) data frame's payload.

        Returns the parsed dict for a valid TEXT message. Returns None --
        after logging a warning -- for a BINARY frame or malformed TEXT,
        which tells the read_message loop to keep waiting.
        """
        if opcode == _OP_TEXT:
            try:
                return json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # F6: named by `self._label`, not hardcoded "event" -- a
                # devices-socket parse problem must not read as a motion-path
                # problem in the log. The default instance's label is still
                # "event", so this text is byte-identical for every existing
                # (events-socket) caller.
                self._logger.warning(
                    "Discarding malformed WS %s frame: %s", self._label, exc)
                return None
        if opcode == _OP_BINARY:
            # On Protect 7.2.105, this socket is plain-text JSON only (the
            # *private* /proxy/protect/ws/updates API is the binary + deflate
            # one -- we deliberately don't use it). If Protect ever switches
            # this socket to binary, every frame is silently dropped, so make
            # that loud instead of quiet. F6: `self._path`/neutral wording so
            # a devices-socket problem doesn't point the debugger at the
            # motion path -- the default (events) instance's own path is
            # still named here, so this text is unchanged for existing callers.
            if not self._binary_warned:
                self._binary_warned = True
                self._logger.warning(
                    "Received a BINARY WS frame on %s -- this endpoint is "
                    "expected to be plain-text JSON on Protect 7.2.105. The "
                    "frame carries nothing usable and is being discarded; "
                    "data may be silently lost from here on.", self._path)
            return None
        return None

    def close(self) -> None:
        """Idempotent. Safe to call on a never-connected instance."""
        if self._sock is None:
            return
        try:
            self._send_frame(_OP_CLOSE)
        except (OSError, ConnectionError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._buffer = b""
        self._frag_opcode = None
        self._frag_payload = bytearray()

    def _take_frame(self) -> Optional[tuple[bool, int, bytes]]:
        """Try to parse and remove one complete frame from the buffer.

        Returns (fin, opcode, unmasked_payload), or None if the buffer does
        not yet hold a complete frame. Handles extended payload lengths 126
        and 127. Server frames are unmasked per RFC6455 (masking below
        applies only to the rare case of a masked server frame).

        Raises ConnectionError if the declared payload length exceeds
        _MAX_FRAME_PAYLOAD -- a corrupt/desynced length must never be
        trusted to grow _buffer without bound.
        """
        buf = self._buffer
        if len(buf) < 2:
            return None

        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2

        if length == 126:
            if len(buf) < offset + 2:
                return None
            length = struct.unpack(">H", buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            length = struct.unpack(">Q", buf[offset:offset + 8])[0]
            offset += 8

        if length > _MAX_FRAME_PAYLOAD:
            raise ConnectionError(
                f"Protect {self._label} socket declared a {length}-byte frame payload, "
                f"exceeding the {_MAX_FRAME_PAYLOAD}-byte cap -- treating the "
                f"stream as corrupt/desynced rather than buffering it")

        mask_key = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask_key = buf[offset:offset + 4]
            offset += 4

        if len(buf) < offset + length:
            return None

        payload = buf[offset:offset + length]
        if masked:
            payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))

        self._buffer = buf[offset + length:]
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """Send one masked client frame (RFC6455 SS5.3 -- client frames MUST
        be masked or a compliant server may drop the connection)."""
        if self._sock is None:
            raise ConnectionError(f"Protect {self._label} socket is not connected")

        length = len(payload)
        fin_and_opcode = 0x80 | (opcode & 0x0F)
        if length < 126:
            header = struct.pack("!BB", fin_and_opcode, 0x80 | length)
        elif length < 0x10000:
            header = struct.pack("!BBH", fin_and_opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", fin_and_opcode, 0x80 | 127, length)

        mask_key = os.urandom(4)
        masked_payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
        try:
            self._sock.sendall(header + mask_key + masked_payload)
        except OSError as exc:
            raise ConnectionError(f"Protect {self._label} socket write failed: {exc}") from None
