"""Server-side player progress for the Bricknet `sync` protocol.

The client never uploads a whole save. It sends *transactions*:

  transactionsPending: [{"id": N, "transactionals": [
      {"type": "cloudObject", "op": "update", "obj": ["<uid>", "save"],
       "value": [13, "key", value, 17, 13, "key2", value2, 17, ...]},
      {"type": "achievement", "op": "set", "obj": "1040", "value": 4},
      {"type": "bank", "op": "add", "obj": "inspirado", "value": 13}]}]
  transactionsVerify:  [N, ...]   # "did you store these?"

and the reply acknowledges them (read by the parser at r2 0x9437b0):

  transactionsProcessed: {"success": [ids], "failed": [ids], "deferred": [ids]}
  transactionsVerified:  {"found": [ids], "checked": [ids], "deferred": [ids]}

Each device gets one file, saves/<deviceId>.json, holding the merged state, plus an
append-only saves/<deviceId>.log.jsonl with every raw transaction, so nothing is lost
even if the merge rules below turn out to be incomplete.
"""
try:
    import fcntl
except ImportError:      # Windows has no fcntl; single-process server, so this is fine
    fcntl = None
import json
import os
import re
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SAVES = os.environ.get("BANDSTARS_SAVES", os.path.join(HERE, "saves"))

# Every device uses the same Bricknet id and token. Both phones already hold these
# in LocalKeyStore, and a different id makes the client throw
# "incorrect bricknetId in response". Saves are keyed by deviceId, not by this id.
BRICKNET_ID = 1000001
TOKEN = "0123456789abcdef0123456789abcdef"

MAX_PROCESSED = 5000

# Acknowledging transactions is required once the client has had any acknowledged:
# with acks switched off (2026-11-03) it sat on "Connecting", polling /utc, waiting
# for its 100 unverified transactions. With acks on, progress was reported to revert
# on relaunch; the cause is not yet known (found/checked/success all move the
# transaction into the same confirmed list), and fixing it needs the cloud-object
# download path (getObjects + versions).
#
# How the sync reply answers the client's transactions:
#   "none"     -> [] for both (original SYNC-v1; client blocks at login once it has
#                 many unverified transactions)
#   "confirm"  -> success / found (lets it log in; progress reverted on relaunch)
#   "deferred" -> every pending and verify id answered as deferred. Logs in
#                 normally, but progress still reverts on relaunch (2026-11-03).
ANSWER = "deferred"

# Download-path experiment (2026-11-03). For these devices only, the sync reply's
# userData.cloudObjectData reports the server's cloud-object versions
# ({owner: {name: version}}), so the client should see newer data and ask for it
# (expected: cloudObject/getObjects). Android 1.4.2 does not use cloud objects at all
# (cloudObjectData is always null), so the test runs on the iPhone (1.9.0), observe-only:
# getObjects is not answered yet.
# Result: the iPhone ignored the reported versions (kept sending version 0, never called
# getObjects), so it is switched off again.
RESTORE_TEST_DEVICES = set()

# Record-delivery test (2026-11-03). The client's sync cloudObjectData {uid: {key: 0}}
# is most likely its cached crc per object, not a version (record parser 0x93d5f4:
# a record is applied when its crc differs from the cached one). So the reply may need
# full records there. For these devices/objects only, send
# {uid: {name: {"crc": RECORD_TEST_CRC, "data": [[13, key, value, ts], ...], "meta": []}}}
# built from the server's merged state. Success = the next sync reports that crc.
# Result 2026-11-03 01:29: the iPhone received the asongs record in two syncs but still
# reported asongs: 0 after relaunch, so it did not take records via sync. Switched off.
RECORD_TEST = {}
RECORD_TEST_CRC = 424242


def _record(obj):
    data = [[FIELD_TAG, key, val, obj.get("ts", {}).get(key, 0)]
            for key, val in obj["fields"].items()]
    return {"crc": RECORD_TEST_CRC, "data": data, "meta": []}

# cloudObject values are flat groups of (13, key, value, ts). The first token is
# always 13; the last grows across uploads (17, 150, 382, 709, ...), so it is a
# per-field timestamp/revision rather than a terminator.
FIELD_TAG = 13


def _path(device, suffix=".json"):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", device or "unknown")
    return os.path.join(SAVES, safe + suffix)


def _empty(device):
    now = int(time.time())
    return {"deviceId": device, "bricknetId": BRICKNET_ID, "created": now, "updated": now,
            "objects": {}, "achievements": {}, "bank": {}, "processed": []}


class Locked:
    """One save file, locked across threads and across the :80/:443 and :8080 servers."""

    def __init__(self, device):
        self.device = device
        os.makedirs(SAVES, exist_ok=True)
        self.lock = open(_path(device, ".lock"), "w")

    def __enter__(self):
        if fcntl:
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        p = _path(self.device)
        self.exists = os.path.exists(p)
        self.data = json.load(open(p, encoding="utf-8")) if self.exists else _empty(self.device)
        return self

    def save(self):
        self.data["updated"] = int(time.time())
        p = _path(self.device)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, p)

    def log(self, entry):
        entry = dict(entry, t=time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(_path(self.device, ".log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def decode_fields(value):
    """[13, k, v, ts, 13, k2, v2, ts2, ...] -> {k: (v, ts), ...}; None if it isn't that shape."""
    if not isinstance(value, list) or len(value) % 4:
        return None
    out = {}
    for i in range(0, len(value), 4):
        tag, key, val, ts = value[i:i + 4]
        if tag != FIELD_TAG or not isinstance(key, str) or not isinstance(ts, int):
            return None
        out[key] = (val, ts)
    return out


def _apply(data, t):
    """Apply one transactional. Returns False for anything not understood (it is still logged)."""
    kind, op = t.get("type"), t.get("op")
    if kind == "cloudObject" and op == "update":
        owner, name = (t.get("obj") or [None, None])[:2]
        fields = decode_fields(t.get("value"))
        if fields is None:
            return False
        obj = data["objects"].setdefault(name, {"owner": owner, "version": 0,
                                                "fields": {}, "ts": {}})
        for key, (val, ts) in fields.items():
            if ts >= obj["ts"].get(key, -1):      # newer (or equal) field revision wins
                obj["fields"][key] = val
                obj["ts"][key] = ts
        obj["owner"] = owner
        obj["version"] += 1
        return True
    if kind == "achievement" and op == "set":
        data["achievements"][str(t.get("obj"))] = t.get("value")
        return True
    if kind == "bank" and op == "add" and isinstance(t.get("value"), (int, float)):
        key = str(t.get("obj"))
        data["bank"][key] = data["bank"].get(key, 0) + t["value"]
        return True
    return False


def sync_result(device, blob):
    """Store what a sync request uploaded and build the matching `result` object."""
    acked, found, asked = [], [], []
    with Locked(device) as s:
        data = s.data
        done = set(data["processed"])
        changed = False
        if isinstance(blob, dict):
            for tx in blob.get("transactionsPending") or []:
                txid = tx.get("id")
                if txid is None:
                    continue
                if txid not in done:
                    unknown = [t for t in tx.get("transactionals", []) if not _apply(data, t)]
                    s.log({"tx": txid, "transactionals": tx.get("transactionals", []),
                           "unapplied": len(unknown)})
                    data["processed"].append(txid)
                    done.add(txid)
                    changed = True
                acked.append(txid)
            # Only ids this server actually stored. Older ids are left unanswered,
            # which is what every reply did before and is known to be harmless.
            asked = list(blob.get("transactionsVerify") or [])
            found = [i for i in asked if i in done]
        if changed:
            data["processed"] = data["processed"][-MAX_PROCESSED:]
            s.save()
        elif not s.exists:
            s.save()
        versions = {}
        records = {}
        for name, obj in data["objects"].items():
            owner = str(obj.get("owner") or BRICKNET_ID)
            versions.setdefault(owner, {})[name] = obj["version"]
            if name in RECORD_TEST.get(device, ()):
                records.setdefault(owner, {})[name] = _record(obj)

    # as in the verified GUEST-v1/SYNC-v1 replies; the userData parser ignores these keys
    user_data = {"level": 1, "coins": 0, "fans": 0}
    if device in RESTORE_TEST_DEVICES and versions:
        user_data["cloudObjectData"] = versions
    elif records:
        user_data["cloudObjectData"] = records

    if ANSWER == "confirm":
        processed = {"success": acked, "failed": [], "deferred": []}
        verified = {"found": found, "checked": [], "deferred": []}
    elif ANSWER == "deferred":
        processed = {"success": [], "failed": [], "deferred": acked}
        verified = {"found": [], "checked": [], "deferred": asked}
    else:
        processed, verified = [], []

    return {
        "user": {"bricknetId": BRICKNET_ID, "displayName": "Player", "isGuest": True},
        "transactionsProcessed": processed,
        "transactionsVerified": verified,
        "sent": 0,
        "userData": user_data,
        "staticData": {},
        "settings": {},
    }


def guest_result(device):
    """Result for a guest login. A login for a device that already has a save is what a
    reinstall looks like, so the save is backed up first: the fresh install's blank
    uploads must not be able to overwrite real progress."""
    with Locked(device) as s:
        if s.exists and s.data["objects"]:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(_path(device), _path(device, f".{stamp}.bak.json"))
            s.log({"event": "guest login on existing save, backed up", "backup": stamp})
    return {
        "sync": sync_result(device, None),
        "id": str(BRICKNET_ID),
        "token": TOKEN,
    }
