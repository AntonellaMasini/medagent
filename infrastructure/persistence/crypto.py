"""Symmetric encryption for sensitive fields (insurer credentials, OAuth tokens)."""
from __future__ import annotations

from cryptography.fernet import Fernet


class CredentialCipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError(
                "SECRET_KEY is required. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
