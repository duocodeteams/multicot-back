from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.companies.schemas import CompanyListResponse, CompanyResponse, CompanyUpdate
from app.companies.service import get_company_by_id, list_companies, update_company
from app.core.database import get_session
from app.core.security import get_current_admin_user
from app.models import User

router = APIRouter()


@router.get("", response_model=CompanyListResponse)
def list_companies_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    active: bool | None = Query(None, description="Si se omite, lista activas e inactivas"),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CompanyListResponse:
    items, total = list_companies(session, active=active, limit=limit, offset=offset)
    return CompanyListResponse(
        items=[CompanyResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/{company_id}", response_model=CompanyResponse)
def patch_company(
    company_id: int,
    data: CompanyUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> CompanyResponse:
    company = update_company(session, company_id, data)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compañía no encontrada")
    return CompanyResponse.model_validate(company)


@router.get("/{company_id}", response_model=CompanyResponse)
def get_company(
    company_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> CompanyResponse:
    company = get_company_by_id(session, company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compañía no encontrada")
    return CompanyResponse.model_validate(company)
