from datetime import datetime

from sqlmodel import Field


class TimestampMixin:
    """Mixin para agregar created_at y updated_at a los modelos.
    No hereda de SQLModel para evitar conflictos de MRO en herencia múltiple."""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime | None = Field(default=None, sa_column_kwargs={"onupdate": datetime.utcnow})
