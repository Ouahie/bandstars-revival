"""Tiny DNS server: sends Halfbrick's server names to your PC, forwards everything else.

usage (run as admin/root, port 53):
  python dns_redirect.py --ip 192.168.1.50

Then on the iPhone: Settings > Wi-Fi > (i) next to your network > Configure DNS >
Manual > remove the existing servers > add your PC's IP. Run server.py on port 80
on the same PC:  python server.py --port 80

Redirected: bricknet.com, bn.tl, halfbrick.com and all their subdomains.
Everything else is forwarded to --upstream (default 1.1.1.1), so the phone keeps working.
Switch the iPhone's DNS back to Automatic when you're done.
"""
import argparse
import socket
import struct
import threading

REDIRECT = ("bricknet.com", "bn.tl", "halfbrick.com")


def qname(data, off=12):
    labels = []
    while data[off]:
        n = data[off]
        labels.append(data[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += n + 1
    return ".".join(labels).lower(), off + 1


def redirected(name):
    return any(name == d or name.endswith("." + d) for d in REDIRECT)


def answer(query, ip):
    name, end = qname(query)
    qtype = struct.unpack(">H", query[end:end + 2])[0]
    header = query[:2] + b"\x81\x80" + query[4:6]          # id, flags: response, no error
    question = query[12:end + 4]
    if qtype != 1:                                          # not an A record: empty answer
        return header + b"\x00\x00\x00\x00\x00\x00" + question
    rr = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(ip)
    return header + b"\x00\x01\x00\x00\x00\x00" + question + rr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True, help="this PC's LAN IP (where server.py runs)")
    ap.add_argument("--upstream", default="1.1.1.1")
    ap.add_argument("--port", type=int, default=53)
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", a.port))
    print(f"DNS on port {a.port}: {', '.join(REDIRECT)} -> {a.ip}; others -> {a.upstream}")

    def forward(data, addr):
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(3)
        try:
            up.sendto(data, (a.upstream, 53))
            sock.sendto(up.recv(4096), addr)
        except OSError:
            pass
        finally:
            up.close()

    while True:
        data, addr = sock.recvfrom(4096)
        try:
            name, _ = qname(data)
        except Exception:
            continue
        if redirected(name):
            print(f"{addr[0]} asked for {name} -> {a.ip}")
            sock.sendto(answer(data, a.ip), addr)
        else:
            threading.Thread(target=forward, args=(data, addr), daemon=True).start()


if __name__ == "__main__":
    main()
