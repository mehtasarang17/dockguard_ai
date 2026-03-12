"""
Encryption helpers for storing sensitive configuration values.

Uses Fernet symmetric encryption with a key derived from the application's SECRET_KEY.
This ensures tenant LLM credentials are encrypted at rest in the database.
"""
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet

from config import Config


def _get_fernet() -> Fernet:
    """Create a Fernet cipher using a key derived from SECRET_KEY."""
    # Derive a 32-byte key from SECRET_KEY using SHA-256
    key = hashlib.sha256(Config.SECRET_KEY.encode()).digest()
    # Fernet requires URL-safe base64 encoding
    fernet_key = base64.urlsafe_b64encode(key)
    return Fernet(fernet_key)


def encrypt_value(plaintext: str) -> Optional[str]:
    """Encrypt a plaintext value.

    Args:
        plaintext: The value to encrypt

    Returns:
        Base64-encoded ciphertext, or None if plaintext is empty
    """
    if not plaintext:
        return None
    fernet = _get_fernet()
    ciphertext = fernet.encrypt(plaintext.encode())
    return ciphertext.decode()


def decrypt_value(ciphertext: str) -> Optional[str]:
    """Decrypt a ciphertext value.

    Args:
        ciphertext: The encrypted value to decrypt

    Returns:
        Decrypted plaintext, or None if ciphertext is empty
    """
    if not ciphertext:
        return None
    fernet = _get_fernet()
    plaintext = fernet.decrypt(ciphertext.encode())
    return plaintext.decode()
