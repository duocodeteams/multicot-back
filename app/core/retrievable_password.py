"""Cifrado reversible para que un admin pueda ver la contraseña (requisito del cliente).

La contraseña sigue guardándose hasheada en `User.password_hash` para login.
`User.password_encrypted` guarda el mismo valor cifrado con Fernet (clave en PASSWORD_ENCRYPTION_KEY).

Sin clave configurada no se persiste ni se puede recuperar el texto claro.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet | None:
    key = (settings.password_encryption_key or "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        return None


def encrypt_for_storage(plain_password: str) -> str | None:
    """Devuelve token Fernet en texto, o None si no hay clave."""
    f = _fernet()
    if f is None:
        return None
    return f.encrypt(plain_password.encode("utf-8")).decode("ascii")


def decrypt_for_admin(stored: str | None) -> str | None:
    """Devuelve la contraseña en claro para respuestas de admin, o None."""
    if not stored:
        return None
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(stored.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None
