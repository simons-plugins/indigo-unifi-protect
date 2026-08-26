#!/usr/bin/env python3
"""Stdlib-only probe of UniFi Protect integration API event socket."""
import base64, json, os, socket, ssl, struct, sys, time

HOST, PORT = "192.168.0.10", 443
PATH = "/proxy/protect/integration/v1/subscribe/events"
KEY = os.environ["PROTECT_KEY"]
RUN_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

raw = socket.create_connection((HOST, PORT), timeout=15)
sock = ctx.wrap_socket(raw, server_hostname=HOST)

nonce = base64.b64encode(os.urandom(16)).decode()
req = (
    f"GET {PATH} HTTP/1.1\r\n"
    f"Host: {HOST}\r\n"
    f"Upgrade: websocket\r\n"
    f"Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {nonce}\r\n"
    f"Sec-WebSocket-Version: 13\r\n"
    f"X-API-KEY: {KEY}\r\n\r\n"
)
sock.sendall(req.encode())

# read handshake response headers
buf = b""
while b"\r\n\r\n" not in buf:
    chunk = sock.recv(4096)
    if not chunk:
        break
    buf += chunk
head, _, rest = buf.partition(b"\r\n\r\n")
print("=== HANDSHAKE ===")
print(head.decode(errors="replace"))
if b"101" not in head.split(b"\r\n")[0]:
    print("!! upgrade failed, body:", rest[:500])
    sys.exit(1)

def recv_exact(n, pending):
    out = pending
    while len(out) < n:
        c = sock.recv(65536)
        if not c:
            raise EOFError("socket closed")
        out += c
    return out[:n], out[n:]

print(f"=== LISTENING {RUN_SECONDS}s ===", flush=True)
sock.settimeout(5)
pending = rest
deadline = time.time() + RUN_SECONDS
count = 0
while time.time() < deadline:
    try:
        hdr, pending = recv_exact(2, pending)
    except (socket.timeout, TimeoutError):
        continue
    except EOFError as e:
        print("closed:", e); break
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    ln = hdr[1] & 0x7F
    if ln == 126:
        ext, pending = recv_exact(2, pending); ln = struct.unpack(">H", ext)[0]
    elif ln == 127:
        ext, pending = recv_exact(8, pending); ln = struct.unpack(">Q", ext)[0]
    if masked:
        mk, pending = recv_exact(4, pending)
    payload, pending = recv_exact(ln, pending) if ln else (b"", pending)
    if opcode == 0x8:
        print("server close frame:", payload[:200]); break
    if opcode == 0x9:
        sock.sendall(b"\x8a\x80" + os.urandom(4)); continue
    if opcode not in (0x1, 0x2):
        continue
    count += 1
    ts = time.strftime("%H:%M:%S")
    is_text = opcode == 0x1
    try:
        msg = json.loads(payload.decode("utf-8"))
        print(f"--- [{ts}] frame {count} opcode={opcode}({'text' if is_text else 'binary'}) len={ln} JSON")
        print(json.dumps(msg, indent=1)[:1500], flush=True)
    except Exception:
        print(f"--- [{ts}] frame {count} opcode={opcode} len={ln} NOT-JSON first32={payload[:32]!r}", flush=True)
print(f"=== DONE: {count} data frames ===")
