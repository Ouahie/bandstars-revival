"""Band Stars stub server (Bricknet replacement).

usage: python server.py [--port 8080] [--https-port 443] [--universe URL] [--key private_key.pem]

Endpoints
  GET  /<anything>.json   the remote config fetched at startup.
                          Replies {"data": b64, "signature": b64, "expire": secs},
                          packed + signed exactly like Halfbrick's.
  POST /<anything>        Bricknet API call (Content-Type application/x-bricknet).
                          Body is decrypted and logged; the reply comes from HANDLERS.
  GET  /                  a small status page (handy on the phone's browser)
  GET  /ca.crt            download the local CA (for installed HTTPS interception)
  GET  /log               the tail of requests.jsonl

The "universe" value in the config is the base URL the game uses for API calls. If
--universe is not given, it is derived from the address the phone actually connected
to, so the phone always gets a URL that works from its point of view.

Everything is printed and appended to requests.jsonl so unknowns can be filled in
from real traffic instead of guesswork.
"""
import argparse
import base64
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bricknet
import progress

HERE = os.path.dirname(os.path.abspath(__file__))

PRIVACY_HTML = """<!doctype html><meta name="viewport" content="width=device-width">
<style>body{font:17px -apple-system,sans-serif;margin:1.5em;line-height:1.6;color:#111}</style>
<h2>Privacy Policy</h2>
<p>This is a private, fan-run replacement server for a discontinued game. It is not
affiliated with Halfbrick.</p>
<p>The only data this server receives is what the game itself sends: a device id,
a product build id and the game's API calls. Nothing is shared with anyone, and
nothing leaves this machine. The request log is kept locally so the server can be
built out, and can be deleted at any time.</p>
<p>Remove the local certificate and set your Wi-Fi DNS back to Automatic when you
stop using it.</p>
"""
LOG = os.path.join(HERE, "requests.jsonl")

# ---------- filled in by main() ----------
PRIVATE_KEY = None
CONFIG = {}          # base config; "universe" is injected per request
UNIVERSE = None      # forced universe URL, or None to derive it
NO_CONFIG = False    # testing: answer the config fetch with 404
UTC_FORMAT = "packed"  # packed | json | text
CLOSE_AFTER_RESPONSE = False  # testing: close the socket after each reply


# ---------- API handlers: method name -> function(request) -> reply dict ----------

def default_reply(req):
    return {"id": req.get("id", 0) if isinstance(req, dict) else 0,
            "result": {}, "errors": []}


# ---- guest login -------------------------------------------------------------
# The request that arrives is method="guest" with params=[{deviceId, productBuildId}].
# Unknown JSON keys are ignored by the client, so the reply deliberately carries
# every plausible spelling of the same information. That beats guessing one shape
# at a time, and the type of "bnid" (number vs string) is the only real fork - so
# the first attempt uses a number and later ones use a string.
GUEST_STATE = {"n": 0}


def _bnid(n: int):
    numeric = 1000001 + n
    return numeric if n == 0 else f"{numeric}"


def guest_reply(req):
    params = (req.get("params") or [{}])[0] if isinstance(req, dict) else {}
    n = GUEST_STATE["n"]
    GUEST_STATE["n"] += 1
    bnid = _bnid(n)
    token = "bnauth-" + "0" * 24 + "01"
    device = params.get("deviceId", "")
    user = {
        "bnid": bnid, "bnauth": token, "isGuest": True,
        "name": "Player", "displayName": "Player", "nickname": "Player",
        "deviceId": device, "buildId": params.get("productBuildId", ""),
        "level": 1, "xp": 0, "avatar": "",
    }
    print(f"     guest attempt {n + 1}: bnid as {type(bnid).__name__}")
    return {
        "id": req.get("id", 0),
        "result": dict(user, user=user, tokens={"bnid": bnid, "token": token},
                       token=token, authDeviceId=device),
        "errors": [],
    }


# ---- persistent guest + sync (progress.py) -----------------------------------
# These replace the static live/guest.json and live/sync.json: same reply shapes,
# but sync now stores every uploaded transaction under saves/<deviceId>.json and
# acknowledges it. They take precedence over live/<method>.json for these methods.

def _device(req):
    """deviceId: guest sends it as params[0].deviceId, sync as params[1]."""
    params = req.get("params") or []
    if params and isinstance(params[0], dict):
        return params[0].get("deviceId", "")
    return params[1] if len(params) > 1 and isinstance(params[1], str) else ""


def persistent_guest(req):
    return {"error": None, "result": progress.guest_result(_device(req))}


def persistent_sync(req):
    params = req.get("params") or []
    blob = params[3] if len(params) > 3 else None
    return {"error": None, "result": progress.sync_result(_device(req), blob)}


HANDLERS = {
    "guest": persistent_guest,
    "sync": persistent_sync,
}
# Replies the client is verified to accept as plain JSON; everything else stays packed.
PLAIN_METHODS = {"guest", "sync"}

# ---- live replies ------------------------------------------------------------
# live/<method>.json overrides HANDLERS, re-read on every request so replies can be
# changed while the game is running (it retries guest every ~11s). Shape:
#   {"variants": [ {"_mode": "pack"|"garbage"|"plain", "_name": "...", ...json...} ]}
# Each request takes the next variant, wrapping around, and the index is logged.
LIVE_DIR = os.path.join(HERE, "live")
LIVE_COUNTER = {}


def _fill(obj, req):
    """Substitute placeholders in values *and* keys, preserving JSON types.

    {id} and {time} fill whole values as numbers; {session} and {deviceId} are
    strings. Keys are substituted too, so a reply can be keyed by the request's
    session id.
    """
    params = req.get("params") or [{}]
    dev = params[0].get("deviceId", "") if params and isinstance(params[0], dict) else ""
    subs = {
        "{id}": str(req.get("id", 0)),
        "{time}": str(int(time.time())),
        "{session}": str(req.get("session", "")),
        "{deviceId}": dev,
        "{ver}": str(req.get("ver", 0)),
        "{locale}": str(req.get("locale", "")),
    }
    numeric = {"{id}", "{time}"}

    def sub_text(s):
        for k, v in subs.items():
            s = s.replace(k, v)
        return s

    def walk(o):
        if isinstance(o, dict):
            return {sub_text(k): walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str):
            if o in numeric:
                return int(subs[o])
            return sub_text(o)
        return o

    return walk(obj)


def live_reply(method, req):
    """Return (raw_bytes, ctype, label) from live/<method>.json, or None."""
    path = os.path.join(LIVE_DIR, f"{method}.json")
    if not os.path.exists(path):
        return None
    try:
        spec = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"     live file unreadable ({e})")
        return None

    variants = spec.get("variants") or [{}]
    i = LIVE_COUNTER.get(method, 0)
    LIVE_COUNTER[method] = i + 1
    v = _fill(dict(variants[i % len(variants)]), req)
    mode = v.pop("_mode", spec.get("mode", "pack"))
    name = v.pop("_name", f"variant {i % len(variants)}")
    # "_payload" allows the reply to be any JSON value (array, string, number),
    # not just an object - needed to test array-shaped responses.
    v = v["_payload"] if "_payload" in v else v

    if mode in ("pack-zeroiv", "pack-ivprepend"):
        import struct as _st, zlib as _zl
        raw = json.dumps(v).encode()
        comp = _zl.compress(raw, 9)
        body = _st.pack("<II", len(comp), len(raw)) + comp
        body += b"\x00" * (16 - len(body) % 16)
        from cryptography.hazmat.primitives.ciphers import Cipher as _C, algorithms as _A, modes as _M
        if mode == "pack-zeroiv":
            c = _C(_A.AES(bricknet.AES_KEY), _M.CBC(b"\x00"*16)).encryptor()
            out = c.update(body) + c.finalize()
        else:
            import os as _os
            iv = _os.urandom(16)
            c = _C(_A.AES(bricknet.AES_KEY), _M.CBC(iv)).encryptor()
            out = iv + c.update(body) + c.finalize()
        return out, "application/x-bricknet", f"{name} ({mode})"

    if mode in ("packed-swapped", "packed-be"):
        # header layout probes: (rawLen, compLen) instead of (compLen, rawLen),
        # or big-endian instead of little-endian
        import struct as _st, zlib as _zl
        raw = json.dumps(v).encode()
        comp = _zl.compress(raw, 9)
        if mode == "packed-swapped":
            body = _st.pack("<II", len(raw), len(comp)) + comp
        else:
            body = _st.pack(">II", len(comp), len(raw)) + comp
        body += b"\x00" * (16 - len(body) % 16)
        return (bricknet.aes_encrypt_raw(body), "application/x-bricknet",
                f"{name} ({mode})", 200)

    if mode == "rawbody":
        return v["text"].encode(), "application/json", f"{name} (raw body)", 200

    if mode == "bomb":
        # packed header claiming a huge decompressed size: if the client processes
        # our packed replies at all, its memory jumps (or it dies).
        import struct as _st, zlib as _zl
        comp = _zl.compress(b'{"a":"A"}')
        body = _st.pack("<II", len(comp), 200*1024*1024) + comp
        body += b"\x00" * (16 - len(body) % 16)
        return (bricknet.aes_encrypt_raw(body), "application/x-bricknet",
                f"{name} (bomb 200MB header)", 200)

    if mode == "empty":
        return b"", "application/x-bricknet", f"{name} (empty 200)", 200
    if mode == "garbage":
        return os.urandom(96), "application/x-bricknet", f"{name} (garbage bytes)", 200
    if mode in ("b64json", "b64json-signed"):
        import base64 as _b64
        raw = json.dumps(v).encode()
        w = {"data": _b64.b64encode(raw).decode()}
        if mode == "b64json-signed":
            w["signature"] = _b64.b64encode(bricknet.sign(PRIVATE_KEY, raw)).decode()
        return json.dumps(w).encode(), "application/json", f"{name} ({mode})", 200

    if mode == "status":
        code = int(v.get("code", 200))
        body = json.dumps(v.get("body", {}))
        ctype = v.get("ctype", "application/json")
        return body.encode(), ctype, f"{name} (HTTP {code})", code

    if mode == "plain-bricknet":
        return json.dumps(v).encode(), "application/x-bricknet", f"{name} (plain json, x-bricknet CT)"

    if mode == "plain":
        return json.dumps(v).encode(), "application/json", f"{name} (plain json)", 200
    if mode in ("wrapped-packed", "wrapped-packed-nosig"):
        import base64 as _b64
        cipher = bricknet.pack_json(v)          # header + zlib payload
        wrapper = {"data": _b64.b64encode(cipher).decode()}
        if mode == "wrapped-packed":
            wrapper["signature"] = _b64.b64encode(
                bricknet.sign(PRIVATE_KEY, cipher)).decode()
        return json.dumps(wrapper).encode(), "application/json", f"{name} ({mode})", 200

    if mode in ("wrapped", "wrapped-nosig"):
        # same envelope the config CDN uses: {"signature": b64, "data": b64(AES(json))}
        import base64 as _b64
        cipher = bricknet.aes_encrypt_zero(json.dumps(v).encode())
        wrapper = {"data": _b64.b64encode(cipher).decode()}
        if mode == "wrapped":
            wrapper["signature"] = _b64.b64encode(
                bricknet.sign(PRIVATE_KEY, cipher)).decode()
        return json.dumps(wrapper).encode(), "application/json", f"{name} ({mode})", 200

    if mode == "raw":
        # config-style: AES over JSON, zero padded, no length header, no zlib
        return (bricknet.aes_encrypt_zero(json.dumps(v).encode()),
                "application/x-bricknet", f"{name} (raw aes)", 200)
    return bricknet.pack_json(v), "application/x-bricknet", f"{name} (packed)", 200


# ---------- plumbing ----------

def log(entry):
    entry["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        f = open(LOG, "a", encoding="utf-8")
    except PermissionError:
        # requests.jsonl was created by a root run; fall back where we can write.
        alt = os.path.join(os.path.dirname(LOG), "requests-as-user.jsonl")
        f = open(alt, "a", encoding="utf-8")
    with f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def local_ip_for(conn) -> str:
    """The address the client connected to, i.e. the URL that works from the phone."""
    try:
        return conn.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "BricknetStub/0.2"
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------

    def send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if CLOSE_AFTER_RESPONSE:
            # Some SDK HTTP layers read until EOF instead of honouring Content-Length.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # replaced by our own output

    def universe(self) -> str:
        if UNIVERSE:
            return UNIVERSE
        port = self.server.server_address[1]
        host = local_ip_for(self.connection)
        return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"

    def real_config(self, path):
        """The original, Halfbrick-signed config captured from conf.cdn.bricknet.com.

        It is authentic and still signature-valid, so the game accepts it. That
        matters because the real schema differs from anything we could guess:
        universe lives at bricknet.universe, and the blob is AES+JSON with no header.
        """
        name = "ios.json" if "2g85y2wtu8is8" in path else "android.json"
        p = os.path.join(HERE, "configs", name)
        if os.path.exists(p):
            return open(p, "rb").read()
        return None

    def build_config(self) -> dict:
        # live/config.json overrides the startup config without restarting the server,
        # so config keys can be tested while the game loops.
        live = os.path.join(LIVE_DIR, "config.json")
        if os.path.exists(live):
            try:
                cfg = json.load(open(live, encoding="utf-8"))
                cfg.pop("_comment", None)
                cfg.setdefault("universe", self.universe())
                # the game reads bricknet.universe; keep it in sync with the address
                # the phone actually reached us on, so this bundle works on any LAN
                if not isinstance(cfg.get("bricknet"), dict):
                    cfg["bricknet"] = {}
                cfg["bricknet"]["universe"] = self.universe()
                return cfg
            except Exception as e:
                print(f"     live config unreadable ({e})")
        cfg = dict(CONFIG)
        cfg["universe"] = self.universe()
        return cfg

    # ---------- GET ----------

    def do_GET(self):
        path = self.path.split("?")[0]

        if path.startswith("/kontagent/"):
            # Halfbrick's dead analytics endpoint (analytics-prod-ingress.bricknet.com).
            # A 404 makes the client retry every few seconds; an empty 200 ends it.
            # Not logged: it is pure telemetry and would flood the logs.
            return self.send(200, b"", "text/plain")

        if NO_CONFIG and path.endswith(".json") and "product.bn.tl" in path:
            # Experiment: pretend the config server is gone and see whether the game
            # falls back to a built-in universe (then only HTTPS interception matters).
            print(f"GET  {self.path} -> 404 (NO_CONFIG test)")
            log({"kind": "config_404", "path": self.path, "headers": dict(self.headers)})
            return self.send(404, b"")

        tls = isinstance(self.connection, ssl.SSLSocket)

        if path in ("/pp", "/privacy"):
            print(f"GET  {path} -> privacy page")
            log({"kind": "privacy", "from": self.client_address[0]})
            return self.send(200, PRIVACY_HTML.encode(), "text/html; charset=utf-8")

        if path == "/utc":
            now = int(time.time())
            print(f"GET  /utc -> {UTC_FORMAT}")
            log({"kind": "utc", "format": UTC_FORMAT, "from": self.client_address[0]})
            if UTC_FORMAT == "text":
                return self.send(200, str(now).encode(), "text/plain")
            if UTC_FORMAT == "json":
                return self.send(200, json.dumps({"utc": now, "time": now}).encode())
            return self.send(200, bricknet.pack_json(
                {"id": 0, "utc": now, "time": now, "serverTime": now,
                 "result": {"utc": now, "time": now, "serverTime": now}}),
                "application/x-bricknet")

        if path == "/ping":
            body = (b"https ok - certificate is trusted\n" if tls
                    else b"http only - this proves nothing about the certificate\n")
            print(f"GET  /ping  ({'https' if tls else 'http'})")
            log({"kind": "ping", "scheme": "https" if tls else "http",
                 "from": self.client_address[0], "headers": dict(self.headers)})
            return self.send(200, body, "text/plain; charset=utf-8")

        if path == "/profile.mobileconfig":
            p = os.path.join(HERE, "certs", "revival-ca.mobileconfig")
            if os.path.exists(p):
                print("GET  /profile.mobileconfig -> CA profile")
                return self.send(200, open(p, "rb").read(),
                                 "application/x-apple-aspen-config")
            return self.send(404, b"no profile yet - run make_profile.py")

        if path == "/ca.crt":
            p = os.path.join(HERE, "certs", "ca.crt")
            if os.path.exists(p):
                print("GET  /ca.crt -> CA certificate")
                return self.send(200, open(p, "rb").read(), "application/x-x509-ca-cert")
            return self.send(404, b"no certs/ yet - run make_certs.py")

        if path == "/log":
            body = b""
            if os.path.exists(LOG):
                body = b"".join(open(LOG, "rb").readlines()[-80:])
            return self.send(200, body, "text/plain; charset=utf-8")

        if path == "/":
            print(f"GET  /  ({'https' if tls else 'http'})")
            log({"kind": "status_page", "scheme": "https" if tls else "http",
                 "from": self.client_address[0], "headers": dict(self.headers)})
            return self.send(200, self.status_page(tls).encode(), "text/html; charset=utf-8")

        static = os.path.normpath(os.path.join(HERE, "static", path.lstrip("/")))
        if static.startswith(os.path.join(HERE, "static")) and os.path.isfile(static):
            print(f"GET  {self.path} -> static file")
            log({"kind": "static", "path": self.path})
            return self.send(200, open(static, "rb").read(), "application/octet-stream")

        # Any *.json is treated as the remote config. The game asks for
        # /<product>.product.bn.tl.json, and the product id differs per build
        # (Android: 1bmcf51plvk00, iOS: 2g85y2wtu8is8), so don't hardcode it.
        if path.endswith(".json"):
            real = self.real_config(path)
            if real is not None:
                print(f"GET  {path} -> genuine Halfbrick-signed config ({len(real)} bytes)")
                log({"kind": "config_real", "path": path, "bytes": len(real),
                     "from": self.client_address[0]})
                return self.send(200, real, "application/json")

            cfg = self.build_config()
            # The config blob is AES(JSON) with NO length header and no zlib -
            # unlike API bodies. Using pack_json() here makes the client reject it.
            blob = bricknet.aes_encrypt_zero(json.dumps(cfg).encode())
            reply = {
                "data": base64.b64encode(blob).decode(),
                "signature": base64.b64encode(bricknet.sign(PRIVATE_KEY, blob)).decode(),
                "expire": 86400,
            }
            print(f"GET  {self.path}")
            print(f"     -> config, universe={cfg['universe']}")
            log({"kind": "config", "path": self.path, "universe": cfg["universe"],
                 "config": cfg, "headers": dict(self.headers)})
            return self.send(200, json.dumps(reply).encode())

        print(f"GET  {self.path} -> 404")
        log({"kind": "unknown_get", "path": self.path, "headers": dict(self.headers),
             "from": self.client_address[0]})
        self.send(404, b"{}")

    # ---------- POST ----------

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n)
        req, decoded = None, None
        try:
            req = bricknet.unpack_json(body)
            decoded = "bricknet"
        except Exception as e:
            decoded = f"undecodable: {e}"
            try:
                req = json.loads(body)
                decoded = "plain json"
            except Exception:
                pass

        method = req.get("method") if isinstance(req, dict) else None

        # guest/sync always use the persistent handlers, so progress is stored even
        # while live/guest.json and live/sync.json exist (older servers still use those)
        live = live_reply(method, req) if method and method not in PLAIN_METHODS else None
        if live:
            raw, ctype, label, code = live
            print(f"POST {self.path}  method={method}  LIVE: {label}")
            log({"kind": "api", "path": self.path, "decoded": decoded,
                 "from": self.client_address[0], "headers": dict(self.headers),
                 "request": req, "reply": raw.hex() if ctype != "application/json" else raw.decode(),
                 "live": label})
            return self.send(code, raw, ctype)

        handler = HANDLERS.get(method)
        reply = handler(req) if handler else default_reply(req)

        print(f"POST {self.path}  method={method or '?'}  [{decoded}]"
              f"{'' if handler else '  (default reply)'}")
        if isinstance(req, dict):
            print("     params:", json.dumps(req.get("params"))[:400])
        log({"kind": "api", "path": self.path, "decoded": decoded,
             "from": self.client_address[0], "headers": dict(self.headers),
             "request": req, "reply": reply,
             "body_hex": None if req is not None else body.hex()})
        if method in PLAIN_METHODS:
            return self.send(200, json.dumps(reply).encode(), "application/json")
        self.send(200, bricknet.pack_json(reply), "application/x-bricknet")

    # ---------- status page ----------

    def status_page(self, tls=False):
        h = self.universe()
        if tls:
            banner = ("<p style='background:#d7f5dd;padding:.6em;border-radius:6px'>"
                      "<b>HTTPS works.</b> This certificate is trusted by this device, "
                      "so the game should now get through.</p>")
        else:
            banner = ("<p style='background:#fde2e1;padding:.6em;border-radius:6px'>"
                      "<b>You are on plain HTTP.</b> This proves nothing about the "
                      "certificate. Open <code>https://api.bricknet.com/ping</code> "
                      "instead.</p>")
        return f"""<!doctype html><meta name="viewport" content="width=device-width">
<style>body{{font:16px -apple-system,sans-serif;margin:2em;line-height:1.5}}
code{{background:#eee;padding:.1em .3em;border-radius:4px}}</style>
<h2>Band Stars revival server</h2>
{banner}
<p>Running. The game should use <code>{h}</code> for its API calls.</p>
<p>Install the certificate profile on the phone, then enable it under<br>
Settings &rsaquo; General &rsaquo; About &rsaquo; Certificate Trust Settings.</p>
<p><a href="/profile.mobileconfig">install CA profile</a> &middot;
<a href="/ca.crt">raw certificate</a> &middot; <a href="/log">request log</a></p>"""


class TLSServer(ThreadingHTTPServer):
    """HTTPS variant. Logs failed handshakes (pinning / untrusted CA)."""

    def __init__(self, addr, handler, certfile, keyfile):
        super().__init__(addr, handler)
        self._local = threading.local()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            self.ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        self.ctx.load_cert_chain(certfile, keyfile)
        self.ctx.sni_callback = self._sni

    def _sni(self, sslobj, name, ctx):
        # sslobj carries no address; the handshake runs on this thread, so this works.
        self._local.name = name

    def finish_request(self, request, client_address):
        self._local.name = None
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            sni = getattr(self._local, "name", None)
            print(f"TLS  {client_address[0]} sni={sni} handshake failed: {e}")
            log({"kind": "tls_fail", "from": client_address[0], "sni": sni, "error": str(e)})
            return
        sni = getattr(tls, "server_hostname", None) or getattr(self._local, "name", None)
        print(f"TLS  {client_address[0]} sni={sni} connected OK")
        log({"kind": "tls_ok", "from": client_address[0], "sni": sni})
        self.RequestHandlerClass(tls, client_address, self)


def main():
    global PRIVATE_KEY, CONFIG, UNIVERSE, NO_CONFIG, UTC_FORMAT, CLOSE_AFTER_RESPONSE

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--https-port", type=int, default=0,
                    help="HTTPS port; needs certs/ from make_certs.py (0 = off)")
    ap.add_argument("--universe", default=None,
                    help="API base URL handed to the game (default: derived per request)")
    ap.add_argument("--key", default=os.path.join(HERE, "private_key.pem"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--log", default=None, help="request log file (default requests.jsonl)")
    ap.add_argument("--close-after-response", action="store_true",
                    help="send Connection: close and close the socket after each reply")
    ap.add_argument("--utc-format", default="packed", choices=["packed", "json", "text"],
                    help="how /utc answers the time poll")
    ap.add_argument("--no-config", action="store_true",
                    help="404 the config fetch, to test the game's built-in fallback")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)   # so the log streams when piped
    except AttributeError:
        pass

    PRIVATE_KEY = bricknet.load_private_key(a.key)
    CONFIG = json.load(open(a.config, encoding="utf-8"))
    CONFIG.pop("_note", None)
    UNIVERSE = a.universe
    NO_CONFIG = a.no_config
    UTC_FORMAT = a.utc_format
    CLOSE_AFTER_RESPONSE = a.close_after_response
    if a.log:
        global LOG
        LOG = a.log

    if a.https_port:
        crt = os.path.join(HERE, "certs", "server.crt")
        key = os.path.join(HERE, "certs", "server.key")
        if os.path.exists(crt):
            tls = TLSServer(("0.0.0.0", a.https_port), Handler, crt, key)
            threading.Thread(target=tls.serve_forever, daemon=True).start()
            print(f"HTTPS on port {a.https_port}")
        else:
            print("HTTPS off: run make_certs.py first")

    print(f"Band Stars stub server on port {a.port}   (log: {LOG})")
    print(f"  config keys: {sorted(CONFIG)}")
    print(f"  universe: {'derived from the connection' if not UNIVERSE else UNIVERSE}")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
