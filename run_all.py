"""Start everything the phone needs, in one process.

    sudo python3 -u run_all.py --ip 192.168.1.50

Ports are privileged, so this needs sudo (or run as root). It starts:
  * DNS on 53      - sends *.bricknet.com / *.bn.tl / *.halfbrick.com here, forwards
                     every other lookup to a real resolver so the phone keeps working
  * HTTP on 80     - the remote config the game fetches at startup
  * HTTPS on 443   - the API. With make_certs.py's CA installed and trusted on the
                     phone, requests to api.bricknet.com land here.

Stop with Ctrl-C; that also puts the phone's DNS back to normal (set the Wi-Fi DNS
back to Automatic).
"""
import argparse
import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dns_redirect          # noqa: E402
import server as stub        # noqa: E402


def start_dns(ip: str, upstream: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))

    def forward(data, addr):
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(3)
        try:
            up.sendto(data, (upstream, 53))
            sock.sendto(up.recv(4096), addr)
        except OSError:
            pass
        finally:
            up.close()

    def loop():
        while True:
            data, addr = sock.recvfrom(4096)
            try:
                name, _ = dns_redirect.qname(data)
            except Exception:
                continue
            if dns_redirect.redirected(name):
                print(f"DNS  {addr[0]} asked for {name} -> {ip}")
                stub.log({"kind": "dns", "from": addr[0], "query": name})
                sock.sendto(dns_redirect.answer(data, ip), addr)
            else:
                threading.Thread(target=forward, args=(data, addr), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    print(f"DNS   on 53: bricknet/bn.tl/halfbrick -> {ip}, rest -> {upstream}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True, help="this Mac's LAN IP, e.g. 192.168.1.50")
    ap.add_argument("--upstream", default="1.1.1.1", help="real DNS for other names")
    ap.add_argument("--universe", default=None,
                    help="API URL to hand the game (default: derived from the request)")
    ap.add_argument("--key", default=os.path.join(HERE, "private_key.pem"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--close-after-response", action="store_true",
                    help="send Connection: close and close the socket after each reply")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    import json
    stub.PRIVATE_KEY = stub.bricknet.load_private_key(a.key)
    stub.CONFIG = json.load(open(a.config, encoding="utf-8"))
    stub.CONFIG.pop("_note", None)
    stub.UNIVERSE = a.universe or f"http://{a.ip}/"
    stub.CLOSE_AFTER_RESPONSE = a.close_after_response

    start_dns(a.ip, a.upstream)

    crt = os.path.join(HERE, "certs", "server.crt")
    key = os.path.join(HERE, "certs", "server.key")
    if os.path.exists(crt):
        tls = stub.TLSServer(("0.0.0.0", 443), stub.Handler, crt, key)
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        print("HTTPS on 443 (cert trusted only if ca.crt is installed + enabled on the phone)")
    else:
        print("HTTPS off - run: python3 make_certs.py")

    print(f"HTTP  on 80: universe = {stub.UNIVERSE}")
    print(f"log: {stub.LOG}\n")
    try:
        ThreadingHTTPServer(("0.0.0.0", 80), stub.Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped - switch the phone's Wi-Fi DNS back to Automatic")


if __name__ == "__main__":
    main()
