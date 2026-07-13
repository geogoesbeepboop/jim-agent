"""At-rest encryption for A2A durable-task secrets.

Three kinds of A2A row hold data that must not sit in the database as plaintext:
the signed x402 ``PaymentPayload`` (a bearer settlement instrument), withheld
monitor memo content (paid artifact held pending settlement), and push configs
(delivery URL + bearer token). All three are Fernet-encrypted here.

Key resolution (first match wins), so a deployment gets restart-stable
decryption with zero new operational burden — mirroring how
``config.py``'s ``monitor_signing_secret`` falls back to the wallet key:

1. ``settings.a2a_encryption_key`` — a valid Fernet key used verbatim, else any
   arbitrary secret string run through HKDF-SHA256 into a Fernet key.
2. else HKDF-SHA256 over ``settings.evm_private_key`` (restart-stable; the wallet
   key already exists in every real deployment).
3. else an ephemeral, process-lifetime random key — fine for MemoryStore dev and
   the offline test suite, but a restart cannot decrypt what a prior process
   wrote, so we warn once.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from jim.config import Settings, get_settings

logger = logging.getLogger(__name__)

_HKDF_INFO = b"jim-a2a-encryption"
_warned_ephemeral = False


def _derive_fernet_key(secret: bytes) -> bytes:
    """HKDF-SHA256(secret) → 32 bytes → urlsafe-b64 → a valid Fernet key."""
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(secret)
    return base64.urlsafe_b64encode(raw)


def _is_fernet_key(value: str) -> bool:
    """True when ``value`` is already a well-formed Fernet key (usable verbatim)."""
    try:
        Fernet(value.encode("utf-8"))
        return True
    except (ValueError, TypeError):
        return False


def resolve_key(settings: Settings | None = None) -> tuple[bytes, str]:
    """Resolve the Fernet key + a short source label (for logging/tests)."""
    settings = settings or get_settings()
    configured = settings.a2a_encryption_key
    if configured:
        if _is_fernet_key(configured):
            return configured.encode("utf-8"), "configured"
        return _derive_fernet_key(configured.encode("utf-8")), "derived-configured"
    if settings.evm_private_key:
        return _derive_fernet_key(settings.evm_private_key.encode("utf-8")), "derived-evm"
    return Fernet.generate_key(), "ephemeral"


class A2ACrypto:
    """Fernet wrapper with JSON/text convenience. Construct once and share; the
    resolved key is fixed for the instance's lifetime."""

    def __init__(self, settings: Settings | None = None) -> None:
        key, source = resolve_key(settings)
        self._fernet = Fernet(key)
        self._source = source
        if source == "ephemeral":
            global _warned_ephemeral
            if not _warned_ephemeral:
                _warned_ephemeral = True
                logger.warning(
                    "A2A encryption key is ephemeral (process-lifetime): set "
                    "A2A_ENCRYPTION_KEY or EVM_PRIVATE_KEY for restart-stable "
                    "decryption of persisted A2A payloads."
                )

    @property
    def source(self) -> str:
        """Where the active key came from: configured / derived-configured /
        derived-evm / ephemeral."""
        return self._source

    def encrypt_text(self, text: str) -> str:
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt_text(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def encrypt_json(self, obj: Any) -> str:
        # sort_keys → stable ciphertext-input ordering; compact separators → smaller blob.
        return self.encrypt_text(json.dumps(obj, separators=(",", ":"), sort_keys=True))

    def decrypt_json(self, token: str) -> dict:
        return json.loads(self.decrypt_text(token))
