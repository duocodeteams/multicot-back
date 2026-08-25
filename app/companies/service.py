from sqlalchemy import func
from sqlmodel import Session, select

from app.models.company import Company

from .schemas import CompanyUpdate


def list_companies(
    session: Session,
    active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Company], int]:
    stmt = select(Company).order_by(Company.id)
    count_stmt = select(func.count()).select_from(Company)
    if active is not None:
        stmt = stmt.where(Company.active == active)
        count_stmt = count_stmt.where(Company.active == active)

    count_result = session.exec(count_stmt).one()
    total = count_result[0] if isinstance(count_result, tuple) else count_result
    items = list(session.exec(stmt.offset(offset).limit(limit)).all())
    return items, total


def get_company_by_id(session: Session, company_id: int) -> Company | None:
    return session.get(Company, company_id)


def update_company(
    session: Session,
    company_id: int,
    data: CompanyUpdate,
) -> Company | None:
    company = session.get(Company, company_id)
    if company is None:
        return None
    company.active = data.active
    session.add(company)
    session.commit()
    session.refresh(company)
    return company
