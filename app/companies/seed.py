"""Seed idempotente de compañías con adapter."""

from sqlmodel import Session, select

from app.models.company import Company

# slug, name (debe coincidir con provider.company_name), active inicial
COMPANY_SEED: tuple[tuple[str, str, bool], ...] = (
    ("pax", "Pax", False),
    ("cardinal", "Cardinal", True),
    ("go_assistance", "GoAssistance", True),
    ("terrawind", "Terrawind", True),
    ("new_travel", "New Travel", True),
    ("inter_assist", "Inter Assist", True),
    ("universal", "Universal", True),
)


def seed_companies(session: Session) -> None:
    """Inserta compañías faltantes por slug. No pisa active/name ya persistidos."""
    for slug, name, active in COMPANY_SEED:
        existing = session.exec(select(Company).where(Company.slug == slug)).first()
        if existing is None:
            session.add(Company(slug=slug, name=name, active=active))
    session.commit()
