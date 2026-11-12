"""Create a local certificate authority + HTTPS certificate for the stub server.

usage: python make_certs.py

Writes into certs/:
  ca.crt / ca.key        your own certificate authority (install ca.crt on the iPhone)
  server.crt / server.key   certificate for *.bricknet.com, *.bn.tl, *.halfbrick.com
Also copies ca.crt to static/ca.crt so the iPhone can download it from
http://<your Mac IP>/ca.crt while server.py is running.

Keep ca.key private, and remove the profile from the iPhone when you're done
(Settings > General > VPN & Device Management).
"""
import datetime
import os
import shutil

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "certs")
NAMES = ["bricknet.com", "*.bricknet.com", "*.cdn.bricknet.com",
         "bn.tl", "*.bn.tl", "*.cdn.bn.tl", "*.product.bn.tl",
         "halfbrick.com", "*.halfbrick.com"]


def write_key(key, path):
    with open(path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))


def main():
    os.makedirs(OUT, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Band Stars Revival Local CA")])
    ca = (x509.CertificateBuilder()
          .subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.bricknet.com")]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=397))  # iOS limit
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in NAMES]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))

    write_key(ca_key, os.path.join(OUT, "ca.key"))
    write_key(key, os.path.join(OUT, "server.key"))
    with open(os.path.join(OUT, "ca.crt"), "wb") as f:
        f.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(OUT, "server.crt"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    os.makedirs(os.path.join(HERE, "static"), exist_ok=True)
    shutil.copy(os.path.join(OUT, "ca.crt"), os.path.join(HERE, "static", "ca.crt"))
    print("wrote certs/ca.crt, certs/server.crt (+ keys) and static/ca.crt")


if __name__ == "__main__":
    main()
