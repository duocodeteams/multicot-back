from sqlmodel import Session, create_engine

from app.core.config import settings
from app.models import Agency, Seller, User  # noqa: F401 - registra los modelos en metadata

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
    echo=settings.environment == "development",
)


def create_db_and_tables() -> None:
    """Crea las tablas en la base de datos. Útil para desarrollo."""
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)


def get_session():
    """Dependency para obtener una sesión de BD en los endpoints."""
    with Session(engine) as session:
        yield session
