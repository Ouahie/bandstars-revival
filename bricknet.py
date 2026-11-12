"""
Bricknet (Halfbrick backend) wire format, as reverse-engineered from
Band Stars v1.4.2 (libmortargame.so).

Packed payload (used by .offline files, the remote config "data" field,
and every API request/response body):

    AES-128-CBC( u32le compressed_len | u32le raw_len | zlib(json) )   PKCS#7 padding
    key = b"10D8AB13CC516B87"   iv = b"AA778145A3498513"   (ASCII, hardcoded in the game)

Signatures (.offline.sig files and the remote config "signature" field):

    RSA-1024 PKCS#1 v1.5 over SHA-512 of the *encrypted* bytes, e = 65537.
    The game holds only the public modulus, so a custom server needs the
    game patched with your own modulus (see patch_game.py).
"""
import json
import struct
import zlib

from cryptography.hazmat.primitives import hashes, padding as sympad, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_KEY = b"10D8AB13CC516B87"
AES_IV = b"AA778145A3498513"

# Halfbrick's original public modulus (0x00 + 128 bytes), as stored in the game.
HALFBRICK_MODULUS = bytes.fromhex(
    "00b6c39e238d5c084772c36778514152d4a0db27961f0ebdc5fd70fd8b56e50b26"
    "637dcff49ae394adcb6eb26860643b930ff0d6a903461cf595cd8a4aaf80738469"
    "37229479000d231ed40d4bb7bd80c65b01935a13c931a38d6225a382c51dfb9d3d"
    "2cbd4d950b023b0d432c362ea8f161e86da92cbda946f76323132542e9eb"
)
PUBLIC_EXPONENT = 65537


# ---------- AES + zlib packing ----------

def aes_encrypt_raw(data: bytes) -> bytes:
    """AES-CBC over data, which must already be a multiple of 16 bytes."""
    if len(data) % 16:
        raise ValueError("data must be a multiple of 16 bytes")
    e = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
    return e.update(data) + e.finalize()


def aes_encrypt(data: bytes) -> bytes:
    p = sympad.PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    return aes_encrypt_raw(data)


def aes_encrypt_zero(raw: bytes) -> bytes:
    """AES-CBC over raw bytes, zero-padded - the format the config blobs use."""
    body = raw + b"\x00" * (16 - len(raw) % 16)
    return aes_encrypt_raw(body)


def aes_decrypt(data: bytes) -> bytes:
    """Decrypt and strip padding.

    Padding is inconsistent in the wild: the bundled .offline files that Halfbrick
    shipped use PKCS#7, but the game's own API requests are zero-padded (seen on
    /auth, 13 zero bytes after the payload). Nothing depends on the pad anyway -
    the u32 length header says exactly how many compressed bytes follow - so accept
    either, and fall through to the unpadded blocks if neither matches.
    """
    d = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).decryptor()
    out = d.update(data) + d.finalize()
    if out and 1 <= out[-1] <= 16 and out.endswith(bytes([out[-1]]) * out[-1]):
        return out[:-out[-1]]          # PKCS#7
    if out.endswith(b"\x00"):
        return out.rstrip(b"\x00")     # zero padding
    return out


def pack(raw: bytes) -> bytes:
    """Pack a payload the way the game's own client does it: zero padding.

    The game zero-pads its requests (observed on /auth: 13 zero bytes after the
    payload) while the .offline files Halfbrick shipped use PKCS#7. Replies must
    match the client's decoder, so replies are zero-padded: always at least one
    zero byte, then to a 16-byte boundary.
    """
    comp = zlib.compress(raw, 9)
    body = struct.pack("<II", len(comp), len(raw)) + comp
    body += b"\x00" * (16 - len(body) % 16)
    return aes_encrypt_raw(body)


def unpack(blob: bytes) -> bytes:
    plain = aes_decrypt(blob)
    clen, rlen = struct.unpack_from("<II", plain)
    raw = zlib.decompress(plain[8:8 + clen])
    if len(raw) != rlen:
        raise ValueError(f"length mismatch: header {rlen}, got {len(raw)}")
    return raw


def pack_json(obj) -> bytes:
    return pack(json.dumps(obj, separators=(",", ":")).encode())


def unpack_json(blob: bytes):
    return json.loads(unpack(blob))


# ---------- RSA signatures ----------

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def generate_private_key(path: str):
    while True:
        k = rsa.generate_private_key(public_exponent=PUBLIC_EXPONENT, key_size=1024)
        if k.public_key().public_numbers().n.bit_length() == 1024:
            break
    with open(path, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    return k


def modulus_bytes(private_key) -> bytes:
    """Modulus in the game's on-disk layout: 0x00 + 128 big-endian bytes."""
    return b"\x00" + private_key.public_key().public_numbers().n.to_bytes(128, "big")


def sign(private_key, data: bytes) -> bytes:
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA512())


def verify_with_modulus(modulus: bytes, data: bytes, sig: bytes) -> bool:
    pub = rsa.RSAPublicNumbers(PUBLIC_EXPONENT, int.from_bytes(modulus, "big")).public_key()
    try:
        pub.verify(sig, data, padding.PKCS1v15(), hashes.SHA512())
        return True
    except Exception:
        return False
