from sqlmodel import Session

from app.core.retrievable_password import encrypt_for_storage
from app.core.security import hash_password
from app.models import User


def update_user_password(session: Session, user_id: int, new_password: str) -> User | None:
    """
    Actualiza hash y copia cifrada recuperable. Retorna None si no existe el usuario.
    """
    user = session.get(User, user_id)
    if user is None:
        return None

    user.password_hash = hash_password(new_password)
    user.password_encrypted = encrypt_for_storage(new_password)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
