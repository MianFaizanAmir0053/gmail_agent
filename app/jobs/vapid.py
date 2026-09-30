"""Generate the VAPID key pair web push signs with (M16, D6).

    .\\tasks.ps1 vapid

The private key goes to Fly as `VAPID_PRIVATE_KEY`, and never anywhere else.
The public key goes to the web app as `NEXT_PUBLIC_VAPID_PUBLIC_KEY`, where
browsers use it to subscribe. Replacing the pair invalidates every existing
subscription; each phone re-subscribes the next time the app opens.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


@dataclass(frozen=True, slots=True)
class VapidKeys:
    private_key: str
    """The raw 32-byte P-256 scalar, base64url: the form `pywebpush` loads."""

    public_key: str
    """The uncompressed point, base64url: a browser's `applicationServerKey`."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_keys() -> VapidKeys:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return VapidKeys(private_key=_b64url(private), public_key=_b64url(public))


def main() -> None:
    keys = generate_keys()
    print("Set on Fly, as a secret, and nowhere else:")
    print(f"  VAPID_PRIVATE_KEY={keys.private_key}")
    print("\nSet in the web app's environment (Vercel):")
    print(f"  NEXT_PUBLIC_VAPID_PUBLIC_KEY={keys.public_key}")


if __name__ == "__main__":
    main()
