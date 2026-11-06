from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_admin_user
from app.credentials.schemas import AgencyCredentialListResponse, AgencyCredentialResponse
from app.credentials.service import (
    CredentialServiceError,
    delete_agency_credential,
    list_agency_credentials,
    upsert_agency_credential,
)
from app.models import User

router = APIRouter()


def _raise_http(exc: CredentialServiceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/{agency_id}/credentials", response_model=AgencyCredentialListResponse)
def get_agency_credentials(
    agency_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> AgencyCredentialListResponse:
    """Credenciales propias de la agencia. Solo ADMIN. Sin fila, usa las genéricas."""
    try:
        items = list_agency_credentials(session, agency_id)
    except CredentialServiceError as exc:
        _raise_http(exc)
    return AgencyCredentialListResponse(items=items)


@router.put(
    "/{agency_id}/credentials/{company_slug}",
    response_model=AgencyCredentialResponse,
)
def put_agency_credential(
    agency_id: int,
    company_slug: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    payload: Annotated[dict[str, Any], Body()],
) -> AgencyCredentialResponse:
    """Guarda la credencial de la agencia para esa compañía. Solo ADMIN.

    Cardinal: ``agente_emisor_guid`` y, si lo hay, ``agente_emisor_secreto``.
    """
    try:
        return upsert_agency_credential(session, agency_id, company_slug, payload)
    except CredentialServiceError as exc:
        _raise_http(exc)


@router.delete(
    "/{agency_id}/credentials/{company_slug}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_agency_credential(
    agency_id: int,
    company_slug: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> None:
    """Borra la credencial propia. La agencia vuelve a las genéricas. Solo ADMIN."""
    try:
        delete_agency_credential(session, agency_id, company_slug)
    except CredentialServiceError as exc:
        _raise_http(exc)
