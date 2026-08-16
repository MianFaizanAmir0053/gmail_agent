"""Materialise credential files from the environment at startup.

The OAuth client secret and the encrypted token are *files*, but a container
gets *environment variables*. `.dockerignore` deliberately keeps both out of the
image -- baking a refresh token into a layer that lands in a registry is how
credentials leak -- so they arrive base64-encoded and are written to disk here.

Only ever writes; never logs contents. A failure to decode is fatal on purpose:
starting without credentials would mean a process that looks healthy and
silently processes no mail.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# env var -> destination path, and the setting that points at it.
SECRET_FILES = (
    ("GOOGLE_CLIENT_SECRETS_B64", "GOOGLE_CLIENT_SECRETS_PATH", "secrets/client_secret.json"),
    ("GOOGLE_TOKEN_B64", "GOOGLE_TOKEN_PATH", "secrets/token.enc"),
)


class SecretDecodeError(RuntimeError):
    """A base64 secret was present but unusable."""


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # Best effort: a no-op on Windows, meaningful on the Linux container.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def materialise_secrets(env: dict[str, str] | None = None) -> list[str]:
    """Decode any `*_B64` credential vars to disk. Returns the paths written.

    Absent variables are skipped rather than defaulted: locally the real files
    already exist, and overwriting them with nothing would be worse than doing
    nothing at all.
    """
    environ = env if env is not None else dict(os.environ)
    written: list[str] = []

    for b64_var, path_var, default_path in SECRET_FILES:
        encoded = environ.get(b64_var)
        if not encoded:
            continue

        destination = Path(environ.get(path_var) or default_path)
        try:
            _write(destination, base64.b64decode(encoded, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise SecretDecodeError(f"{b64_var} is not valid base64") from exc

        written.append(str(destination))
        log.info("wrote %s from %s", destination, b64_var)

    return written
