from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.agencies.schemas import (
    AgencyCreate,
    AgencyListResponse,
    AgencyResponse,
    AgencyUpdate,
    AgencyUserResponse,
)
from app.agencies.service import (
    create_agency_with_user,
    delete_agency_logical,
    get_agency_by_id,
    list_agencies,
    update_agency,
)
from app.core.database import get_session
from app.core.retrievable_password import decrypt_for_admin
from app.core.security import get_current_admin_user
from app.models import User
from sqlmodel import Session

router = APIRouter()


def _agency_to_response(agency, user) -> AgencyResponse:
    """Construye AgencyResponse desde agency y user."""
    user_resp = None
    if user:
        user_resp = AgencyUserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            password=decrypt_for_admin(user.password_encrypted),
        )
    return AgencyResponse(
        id=agency.id,
        name=agency.name,
        legal_name=agency.legal_name,
        tax_id=agency.tax_id,
        address=agency.address,
        country=agency.country,
        legal_representative_name=agency.legal_representative_name,
        agency_email=agency.agency_email,
        administration_email=agency.administration_email,
        office_phone=agency.office_phone,
        contact_name=agency.contact_name,
        contact_email=agency.contact_email,
        contact_phone=agency.contact_phone,
        activation_date=agency.activation_date,
        commission=agency.commission,
        billing_frequency=agency.billing_frequency,
        payment_method=agency.payment_method,
        tax_condition=agency.tax_condition,
        bank_account=agency.bank_account,
        ssn_register=agency.ssn_register,
        user=user_resp,
    )


@router.post("", response_model=AgencyResponse, status_code=status.HTTP_201_CREATED)
def create_agency(
    data: AgencyCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> AgencyResponse:
    """Crea una agencia con su usuario principal. Solo ADMIN."""
    try:
        agency, user = create_agency_with_user(session, data)
        return _agency_to_response(agency, user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("", response_model=AgencyListResponse)
def list_agencies_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgencyListResponse:
    """Lista agencias activas con paginación. Solo ADMIN."""
    items_tuples, total = list_agencies(session, limit=limit, offset=offset)
    items = [_agency_to_response(agency, user) for agency, user in items_tuples]
    return AgencyListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{agency_id}", response_model=AgencyResponse)
def get_agency(
    agency_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> AgencyResponse:
    """Obtiene una agencia por ID. Solo ADMIN. 404 si está inactiva."""
    result = get_agency_by_id(session, agency_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agencia no encontrada")
    agency, user = result
    return _agency_to_response(agency, user)


@router.patch("/{agency_id}", response_model=AgencyResponse)
def patch_agency(
    agency_id: int,
    data: AgencyUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> AgencyResponse:
    """Actualiza una agencia. Solo ADMIN. 404 si está inactiva."""
    agency = update_agency(session, agency_id, data)
    if agency is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agencia no encontrada")
    result = get_agency_by_id(session, agency_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agencia no encontrada")
    agency, user = result
    return _agency_to_response(agency, user)


@router.delete("/{agency_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agency(
    agency_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> None:
    """Borrado lógico: desactiva la agencia y usuarios/vendedores asociados. Solo ADMIN."""
    if not delete_agency_logical(session, agency_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agencia no encontrada")
