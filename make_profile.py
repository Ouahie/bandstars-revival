"""Build an iOS configuration profile containing the local CA.

    python3 make_profile.py

Writes certs/revival-ca.mobileconfig. The server hands it out at
http://<mac-ip>/profile.mobileconfig, so the phone only has to open that URL.

Why a profile instead of the bare .crt: on current iOS a raw certificate download
often lands in Files instead of the certificate store, whereas a configuration
profile reliably prompts "Profile Downloaded" and installs the root into the trust
store. The profile is unsigned, so iOS shows an "Unsigned" warning - expected and
fine for a root you made yourself.

Installing is only half of it. You must also switch it on under
Settings > General > About > Certificate Trust Settings. Without that the app's TLS
handshake still fails and the log keeps showing tls_fail.
"""
import os
import plistlib
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
CA_PEM = os.path.join(HERE, "certs", "ca.crt")
OUT = os.path.join(HERE, "certs", "revival-ca.mobileconfig")

# Stable identifiers, so re-installing replaces the old profile instead of stacking them.
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def main():
    with open(CA_PEM, "rb") as f:
        ca = x509.load_pem_x509_certificate(f.read())

    fingerprint = ca.fingerprint(hashes.SHA256()).hex()
    name = ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": "com.bandstars.revival.profile",
        "PayloadUUID": str(uuid.uuid5(NS, "profile")),
        "PayloadDisplayName": "Band Stars Revival CA",
        "PayloadDescription": "Trusts the local server that replaces Halfbrick's "
                              "shut-down Bricknet backend. Remove it when you're done.",
        "PayloadOrganization": "Band Stars revival",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root",
            "PayloadVersion": 1,
            "PayloadIdentifier": "com.bandstars.revival.ca",
            "PayloadUUID": str(uuid.uuid5(NS, fingerprint)),
            "PayloadDisplayName": name,
            "PayloadCertificateFileName": "ca.crt",
            "PayloadContent": ca.public_bytes(serialization.Encoding.DER),
        }],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        plistlib.dump(profile, f)
    print(f"wrote {OUT}  ({os.path.getsize(OUT)} bytes)")
    print(f"  root:        {name}")
    print(f"  fingerprint: {fingerprint}")


if __name__ == "__main__":
    main()
