"""Make an APK debuggable by swapping a same-length, expendable attribute name
in the manifest's string pool - no string-pool resizing, no index renumbering.

`launchMode` (10 chars, index 8) is replaced by `debuggable` (10 chars) in place,
that map slot is repointed to android.R.attr.debuggable, and a boolean attribute
is appended to <application>. Because nothing changes size, every other string
index stays valid, which is exactly what the platform needs.

usage: python make_debuggable3.py IN.apk OUT.apk
"""
import struct
import sys
import zipfile

RES_STRING_POOL = 0x0001
RES_XML_RESOURCE_MAP = 0x0180
CH_START_EL = 0x0102
TYPE_INT_BOOLEAN = 0x12
ATTR_DEBUGGABLE = 0x01010F if False else 0x0101000F


def patch(xml):
    ctype, hsize, size, count, style_count, flags, strings_start, styles_start = \
        struct.unpack_from("<HHIIIIII", xml, 8)
    assert ctype == RES_STRING_POOL
    assert not (flags & 0x100), "expected a UTF-16 pool"
    offsets = struct.unpack_from("<%dI" % count, xml, 8 + hsize)
    base = 8 + strings_start

    victim = None
    for i, o in enumerate(offsets):
        p = base + o
        n = struct.unpack_from("<H", xml, p)[0]
        s = xml[p + 2:p + 2 + n * 2].decode("utf-16-le")
        if s == "launchMode":
            victim = (i, p, n)
            break
    if victim is None:
        raise SystemExit("launchMode not found in pool")
    i_name, p_name, n_chars = victim
    if n_chars != len("debuggable"):
        raise SystemExit("length mismatch, cannot swap in place")
    print(f"swapping pool string {i_name} {n_chars} chars: launchMode -> debuggable")

    out = bytearray(xml)
    out[p_name + 2:p_name + 2 + n_chars * 2] = "debuggable".encode("utf-16-le")

    map_pos = 8 + size
    mtype, mhsize, msize = struct.unpack_from("<HHI", out, map_pos)
    assert mtype == RES_XML_RESOURCE_MAP
    struct.pack_into("<I", out, map_pos + mhsize + 4 * i_name, ATTR_DEBUGGABLE)
    print(f"resource map slot {i_name} -> 0x{ATTR_DEBUGGABLE:08x}")

    # locate the android namespace URI string index
    i_ns = None
    for i, o in enumerate(offsets):
        p = base + o
        n = struct.unpack_from("<H", out, p)[0]
        s = out[p + 2:p + 2 + n * 2].decode("utf-16-le")
        if s == "http://schemas.android.com/apk/res/android":
            i_ns = i
            break

    tail_start = map_pos + msize
    pos = tail_start
    target = None
    while pos + 8 <= len(out):
        ct, ch, cs = struct.unpack_from("<HHI", out, pos)
        if ct == CH_START_EL:
            ns, name = struct.unpack_from("<II", out, pos + ch)
            p = base + offsets[name]
            n = struct.unpack_from("<H", out, p)[0]
            if out[p + 2:p + 2 + n * 2].decode("utf-16-le") == "application":
                target = pos
                break
        if cs < 8:
            break
        pos += cs
    if target is None:
        raise SystemExit("<application> not found")

    astart, asize, acount = struct.unpack_from("<HHH", out, target + 16 + 8)
    attr_base = target + 16 + astart

    # aapt keeps element attributes sorted by resource id; the platform relies on it.
    insert_at = attr_base + acount * asize
    for i in range(acount):
        a = attr_base + i * asize
        aname = struct.unpack_from("<I", out, a + 4)[0]
        if aname == 0xFFFFFFFF:
            continue
        attr_id = struct.unpack_from("<I", out, map_pos + mhsize + 4 * aname)[0]
        if attr_id > ATTR_DEBUGGABLE:
            insert_at = a
            print(f"inserting before attribute {i} (0x{attr_id:08x}) to keep id order")
            break

    out[insert_at:insert_at] = struct.pack("<IIIHBBI", i_ns, i_name, 0xFFFFFFFF,
                                           8, 0, TYPE_INT_BOOLEAN, 0xFFFFFFFF)
    struct.pack_into("<H", out, target + 16 + 8 + 4, acount + 1)
    struct.pack_into("<I", out, target + 4,
                     struct.unpack_from("<I", out, target + 4)[0] + 20)
    struct.pack_into("<I", out, 4, len(out))
    return bytes(out), i_name


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with zipfile.ZipFile(src) as zin:
        patched, idx = patch(zin.read("AndroidManifest.xml"))
        with zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                data = patched if info.filename == "AndroidManifest.xml" else zin.read(info.filename)
                zout.writestr(info, data)
    print("wrote", dst)


if __name__ == "__main__":
    main()
