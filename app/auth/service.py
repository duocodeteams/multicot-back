from sqlmodel import Session, select

from app.core.security import verify_password
from app.models import User


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    statement = select(User).where(User.email == email, User.active == True)
    user = session.exec(statement).first()
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
