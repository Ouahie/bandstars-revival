# Band Stars revival — research bundle

A self-hosted replacement backend for **Band Stars** (Halfbrick, discontinued).
The game's own servers are gone; this bundle answers them locally so the game runs
again on a phone you already own.

- Android build: **1.4.2** (patched APK required, tools included)
- iOS build: **1.9.0** (stock App Store app; needs DNS + a trusted certificate)

> **Not included: the game itself.** The APK/OBB/IPA are copyrighted and you must
> supply your own copy. The tools here only modify files you already have.

---

## 0. Requirements

- **Python 3.9 or newer** (macOS: `python3 --version`; Windows: `py -3 --version`)
- `pip install -r requirements.txt` (only `cryptography` is needed)
- A phone and a computer on the **same Wi-Fi network**

Unzip this bundle somewhere writable and run everything from inside that folder.

---

## A. Android (easiest — no administrator rights, no DNS changes)

`patch_game.py` rewrites the game's hard-coded config URLs to point at your computer
and re-signs the offline data with a key you control, so no DNS interception is involved.

**1. Patch your own copy.** You need the 1.4.2 APK and its OBB:

```
python3 patch_game.py --apk Band-Stars-v1-4-2.apk \
                      --obb main.1410.com.halfbrick.bandstars.obb \
                      --url http://192.168.1.50:8080/ \
                      --out patched/
```
Replace `192.168.1.50` with **your computer's LAN IP**:
- macOS: `ipconfig getifaddr en0`
- Windows: `ipconfig` → "IPv4 Address"

This produces `patched/` with a rebuilt, signed APK and a re-signed OBB, and uses
(or creates) `private_key.pem`.

**2. Start the server** (no `sudo` needed, port 8080 is unprivileged):

```
python3 server.py --port 8080
```

**3. Install on the phone:**

```
adb install -r patched/*.apk
adb push patched/*.obb /sdcard/Android/obb/com.halfbrick.bandstars/
```
(or copy the OBB there with a file manager — the folder name must match exactly)

**4. Launch the game.** It should reach your computer directly.

---

## B. iPhone (stock app — needs administrator rights and DNS)

The App Store build is not patched, so it still asks for the original hostnames.
This bundle intercepts DNS for them and serves the config the stock app accepts.

**1. Generate and trust your own certificate.** Run `python3 make_certs.py` and
`python3 make_profile.py` to create a local certificate authority and iPhone profile.
Then send `certs/revival-ca.mobileconfig` to the iPhone (AirDrop/email), install the
profile, then go to
**Settings → General → About → Certificate Trust Settings** and enable full trust for it.

**2. Point the phone's DNS at your computer:** Settings → Wi-Fi → (your network) →
Configure DNS → Manual → add your computer's LAN IP.

**3. Start the server** (needs root — it binds ports 53/80/443):

```
sudo python3 -u run_all.py --ip 192.168.1.50     # macOS / Linux
```
```
# Windows: open an Administrator terminal, then
py -3 -u run_all.py --ip 192.168.1.50
```

**4. Launch the game.**

**iPhone gotchas**
- **iCloud Private Relay** must be off (Settings → your name → iCloud → Private Relay),
  or DNS is bypassed and nothing resolves.
- If the game hangs on **"Connecting…"**, the phone cannot reach port 53/80/443 —
  check the IP, the firewall prompt, and that the server is still running.
- When you're done, set the Wi-Fi DNS back to **Automatic**.

---

## What the server stores

- `saves/<deviceId>.json` — merged progress per device
- `saves/<deviceId>.log.jsonl` — every raw transaction received
- `requests.jsonl` — request log (useful when debugging a device)

`live/guest.json`, `live/sync.json` and `live/config.json` are the reply templates;
they are re-read on every request, so you can edit them while the server runs.

---

## Notes and caveats

- **Ports:** Android path uses only **8080**. iOS path uses **53** (DNS), **80**
  (config) and **443** (API). Allow the server through the OS firewall when prompted.
- **`private_key.pem`** signs the config and replies that the *patched Android* build
  trusts. If you generate your own (`python3 patch_game.py --key mykey.pem …`), the
  server must use the same key: `python3 server.py --port 8080 --key mykey.pem`.
- **The iOS path uses `configs/ios.json`**, which is the original Halfbrick-signed
  config — that is what makes the unpatched app accept the server.
- **Don't redistribute the game files.** Share this bundle; each person supplies their
  own APK/OBB/IPA.
- This is a research/preservation project. It is not affiliated with Halfbrick.

---

## Troubleshooting

| symptom | likely cause |
|---|---|
| Android: "Connecting…" forever | wrong IP in `--url`, server not running, or phone not on the same Wi-Fi |
| Android: app crashes on launch | OBB missing/misnamed under `Android/obb/com.halfbrick.bandstars/` |
| iPhone: nothing resolves | DNS not set to the computer, Private Relay on, or port 53 blocked |
| iPhone: certificate warning | the CA profile isn't installed **and trusted**, or the server cert was regenerated — reinstall the profile |
| Both: login loops every ~11 s | server not answering `/auth` — check `requests.jsonl` and that `live/guest.json` exists |
