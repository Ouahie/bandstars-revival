"""Patch Band Stars v1.4.2 to trust your own server.

usage:
  python patch_game.py --apk Band-Stars-v1-4-2.apk \
                       --obb main.1410.com.halfbrick.bandstars.obb \
                       --url http://10.0.2.2:8080/ \
                       --out patched/

What it changes:
  1. In every libmortargame.so (all 4 CPU builds):
     - replaces Halfbrick's RSA public modulus with yours (private_key.pem,
       created on first run), so the game accepts data your server signs;
     - replaces both config URLs (http://conf.cdn.bricknet.com/ and
       http://conf.cdn.bn.tl/) with --url, so no DNS tricks are needed.
  2. In the OBB: re-signs every *.offline file with your key and updates the
     matching CRC32 lines in crcFile.txt (otherwise the bundled offline data
     would fail the new key check).
  3. Rebuilds the APK and signs it with a local debug keystore (jarsigner,
     ships with any JDK).

--url must be at most 22 characters (the space available in the game's
string slot), end with "/", and be plain http://. Examples:
  http://10.0.2.2:8080/      Android Studio emulator -> your PC
  http://192.168.1.50/       phone on your Wi-Fi -> PC on port 80
"""
import argparse
import os
import shutil
import subprocess
import zipfile
import zlib

import bricknet

ORIG_URLS = [b"http://conf.cdn.bricknet.com/", b"http://conf.cdn.bn.tl/"]
MAX_URL = 22  # smallest slot across all builds (x86, conf.cdn.bn.tl)


def patch_lib(data: bytes, new_modulus: bytes, url: bytes) -> bytes:
    n = data.count(bricknet.HALFBRICK_MODULUS)
    if n == 0:
        raise SystemExit("Halfbrick modulus not found - unexpected game version?")
    data = data.replace(bricknet.HALFBRICK_MODULUS, new_modulus)
    out = bytearray(data)
    for orig in ORIG_URLS:
        i = out.find(orig + b"\0")
        if i < 0:
            raise SystemExit(f"{orig.decode()} not found - unexpected game version?")
        out[i:i + len(orig)] = url.ljust(len(orig), b"\0")
    return bytes(out), n


def rebuild_zip(src, dst, replace):
    """Copy a zip, replacing some entries, keeping each entry's compression."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", compresslevel=9) as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith("META-INF/") and name.upper().endswith((".SF", ".RSA", ".DSA", ".EC", "MANIFEST.MF")):
                continue  # old APK signature, re-signed below
            data = replace.get(name)
            if data is None:
                data = zin.read(name)
            new = zipfile.ZipInfo(name, date_time=info.date_time)
            new.compress_type = info.compress_type
            new.external_attr = info.external_attr
            zout.writestr(new, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True)
    ap.add_argument("--obb", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", default="private_key.pem")
    ap.add_argument("--out", default="patched")
    a = ap.parse_args()

    url = a.url.encode()
    if not (url.startswith(b"http://") and url.endswith(b"/")) or len(url) > MAX_URL:
        raise SystemExit(f"--url must start with http://, end with /, and be <= {MAX_URL} chars "
                         f"(got {len(url)}). Tip: run the server on port 80 to drop ':port'.")

    if os.path.exists(a.key):
        key = bricknet.load_private_key(a.key)
        print(f"using existing key {a.key}")
    else:
        key = bricknet.generate_private_key(a.key)
        print(f"generated new key {a.key}  (keep it: the server needs the same one)")
    modulus = bricknet.modulus_bytes(key)
    os.makedirs(a.out, exist_ok=True)

    # 1. native libraries
    replace = {}
    with zipfile.ZipFile(a.apk) as z:
        for name in z.namelist():
            if name.endswith("libmortargame.so"):
                patched, n = patch_lib(z.read(name), modulus, url)
                replace[name] = patched
                print(f"patched {name}: {n} key copies, 2 URLs")

    unsigned = os.path.join(a.out, "unsigned.apk")
    out_apk = os.path.join(a.out, "BandStars-patched.apk")
    rebuild_zip(a.apk, unsigned, replace)

    # 3. sign APK
    ks = os.path.join(a.out, "debug.keystore")
    if not os.path.exists(ks):
        subprocess.run(["keytool", "-genkeypair", "-keystore", ks, "-storepass", "android",
                        "-keypass", "android", "-alias", "bandstars", "-keyalg", "RSA",
                        "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=Band Stars Revival"], check=True, capture_output=True)
    shutil.copy(unsigned, out_apk)
    subprocess.run(["jarsigner", "-keystore", ks, "-storepass", "android",
                    "-sigalg", "SHA256withRSA", "-digestalg", "SHA-256",
                    out_apk, "bandstars"], check=True, capture_output=True)
    os.remove(unsigned)
    print(f"wrote {out_apk}")

    # 2. OBB: re-sign offline files, fix crcFile.txt
    obb_replace = {}
    with zipfile.ZipFile(a.obb) as z:
        names = z.namelist()
        crc_name = next((n for n in names if n.endswith("crcFile.txt")), None)
        prefix = crc_name.rsplit("/", 1)[0] + "/" if crc_name and "/" in crc_name else ""
        new_crcs = {}
        for name in names:
            if name.endswith(".offline"):
                sig = bricknet.sign(key, z.read(name))
                obb_replace[name + ".sig"] = sig
                new_crcs[name[len(prefix):] + ".sig"] = zlib.crc32(sig)
        if crc_name:
            text = z.read(crc_name).decode()
            eol = "\r\n" if "\r\n" in text else "\n"
            lines = []
            for line in text.splitlines():
                fname = line.rsplit(":", 1)[0]
                lines.append(f"{fname}:{new_crcs[fname]}" if fname in new_crcs else line)
            obb_replace[crc_name] = (eol.join(lines) + (eol if text.endswith(("\n", "\r")) else "")).encode()
    out_obb = os.path.join(a.out, os.path.basename(a.obb))
    rebuild_zip(a.obb, out_obb, obb_replace)
    print(f"re-signed {len(new_crcs)} offline files, wrote {out_obb}")


if __name__ == "__main__":
    main()
