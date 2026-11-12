import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PiiCipher:
    """AES-256-GCM helper for selectively encrypted PII columns.

    A production deployment must provide a distinct 32-byte base64 key via a
    secrets manager. Development intentionally generates an ephemeral key.
    """

    def __init__(self, encoded_key: str | None) -> None:
        self._key = (
            base64.urlsafe_b64decode(encoded_key)
            if encoded_key
            else hashlib.sha256(b"kaziflow-development-key-not-for-production").digest()
        )
        if len(self._key) != 32:
            raise ValueError("PII_ENCRYPTION_KEY must decode to exactly 32 bytes")

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, value.encode(), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        raw = base64.urlsafe_b64decode(value)
        return AESGCM(self._key).decrypt(raw[:12], raw[12:], None).decode()

    def fingerprint(self, value: str) -> str:
        """Stable keyed lookup token; avoids indexing plaintext phone/WhatsApp IDs."""

        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()


def valid_meta_signature(raw_body: bytes, received_signature: str | None, app_secret: str) -> bool:
    """Validate Meta's ``X-Hub-Signature-256`` in constant time."""

    if not received_signature or not received_signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received_signature.removeprefix("sha256="), expected)
