from __future__ import annotations

import base64
import hashlib
import hmac
import os


class SessionCipher:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is required.")
        self.key = hashlib.sha256(secret.encode("utf-8")).digest()

    def encrypt(self, value: str) -> str:
        nonce = os.urandom(16)
        data = value.encode("utf-8")
        stream = self._stream(nonce, len(data))
        encrypted = bytes(left ^ right for left, right in zip(data, stream, strict=True))
        mac = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + mac + encrypted).decode("ascii")

    def decrypt(self, value: str) -> str:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        if len(raw) < 48:
            raise ValueError("Invalid encrypted session payload.")
        nonce = raw[:16]
        mac = raw[16:48]
        encrypted = raw[48:]
        expected = hmac.new(self.key, nonce + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("Encrypted session payload failed authentication.")
        stream = self._stream(nonce, len(encrypted))
        data = bytes(left ^ right for left, right in zip(encrypted, stream, strict=True))
        return data.decode("utf-8")

    def _stream(self, nonce: bytes, size: int) -> bytes:
        chunks: list[bytes] = []
        counter = 0
        while sum(len(chunk) for chunk in chunks) < size:
            counter_bytes = counter.to_bytes(8, "big")
            chunks.append(hmac.new(self.key, nonce + counter_bytes, hashlib.sha256).digest())
            counter += 1
        return b"".join(chunks)[:size]
