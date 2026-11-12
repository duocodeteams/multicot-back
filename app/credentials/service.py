"""Credenciales de agencia por compañía.

Hoy el único formato es Cardinal. El resto de las compañías, Omint incluido,
sigue leyendo Settings. Una agencia sin fila usa esas credenciales genéricas.
Los vendedores usan la credencial de su agencia.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.retrievable_password import decrypt_for_admin, encrypt_for_storage
from app.credentials.schemas import (
    CREDENTIAL_WRITES,
    AgencyCredentialResponse,
    CardinalCredentials,
)
from app.models.agency import Agency
from app.models.company import Company
from app.models.company_credential import CompanyCredential
from app.models.seller import Seller
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


class CredentialServiceError(Exception):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def list_agency_credentials(session: Session, agency_id: int) -> list[AgencyCredentialResponse]:
    _require_agency(session, agency_id)
    items: list[AgencyCredentialResponse] = []
    for slug in CREDENTIAL_WRITES:
        company = _company_by_slug(session, slug)
        row = _find_credential(session, agency_id, company.id) if company is not None else None
        if row is None or not row.active:
            items.append(
                AgencyCredentialResponse(
                    company_slug=slug,
                    company_name=company.name if company is not None else slug,
                    uses_default=True,
                )
            )
            continue
        items.append(
            AgencyCredentialResponse(
                company_slug=slug,
                company_name=company.name if company is not None else slug,
                uses_default=False,
                credentials=_decrypt_payload(row.payload_encrypted),
            )
        )
    return items


def upsert_agency_credential(
    session: Session,
    agency_id: int,
    company_slug: str,
    payload: dict,
) -> AgencyCredentialResponse:
    _require_agency(session, agency_id)
    parsed = _parse_payload(company_slug, payload)
    company = _company_by_slug(session, company_slug)
    if company is None:
        raise CredentialServiceError("Compañía no cargada", status_code=404)

    stored = encrypt_for_storage(parsed.model_dump_json())
    if stored is None:
        raise CredentialServiceError(
            "Falta PASSWORD_ENCRYPTION_KEY para guardar credenciales",
            status_code=500,
        )

    row = _find_credential(session, agency_id, company.id)
    if row is None:
        row = CompanyCredential(
            agency_id=agency_id,
            company_id=company.id,
            payload_encrypted=stored,
            active=True,
        )
    else:
        row.payload_encrypted = stored
        row.active = True
        row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return AgencyCredentialResponse(
        company_slug=company.slug,
        company_name=company.name,
        uses_default=False,
        credentials=parsed.model_dump(),
    )


def delete_agency_credential(session: Session, agency_id: int, company_slug: str) -> None:
    _require_agency(session, agency_id)
    if company_slug not in CREDENTIAL_WRITES:
        raise CredentialServiceError(
            "Esta compañía todavía no tiene credenciales propias",
            status_code=404,
        )
    company = _company_by_slug(session, company_slug)
    row = _find_credential(session, agency_id, company.id) if company is not None else None
    if row is None:
        raise CredentialServiceError("La agencia no tiene credencial propia", status_code=404)
    session.delete(row)
    session.commit()


def resolve_cardinal_credentials(session: Session, user: User | None) -> CardinalCredentials:
    """Credencial de la agencia del usuario, o la genérica de Settings."""
    return resolve_cardinal_credentials_for_agency(session, agency_id_for_user(session, user))


def resolve_cardinal_credentials_for_agency(
    session: Session,
    agency_id: int | None,
) -> CardinalCredentials:
    """Credencial de esa agencia, o la genérica de Settings si no tiene fila."""
    if agency_id is None:
        return _cardinal_from_settings()

    company = _company_by_slug(session, "cardinal")
    if company is None:
        return _cardinal_from_settings()
    row = _find_credential(session, agency_id, company.id)
    if row is None or not row.active:
        return _cardinal_from_settings()

    plain = decrypt_for_admin(row.payload_encrypted)
    if not plain:
        logger.error(
            "Cardinal: no se pudo leer la credencial de la agencia %s",
            agency_id,
        )
        return CardinalCredentials()
    try:
        data = json.loads(plain)
        return CardinalCredentials.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        logger.error(
            "Cardinal: credencial inválida de la agencia %s",
            agency_id,
        )
        return CardinalCredentials()


def agency_id_for_user(session: Session, user: User | None) -> int | None:
    if user is None:
        return None
    if user.role == UserRole.AGENCY:
        return user.agency_id
    if user.role == UserRole.SELLER:
        seller = session.exec(select(Seller).where(Seller.user_id == user.id)).first()
        if seller is None:
            return None
        return seller.agency_id
    return None


def _cardinal_from_settings() -> CardinalCredentials:
    settings = get_settings()
    return CardinalCredentials(
        agente_emisor_guid=settings.cardinal_agente_emisor_guid,
        agente_emisor_secreto=settings.cardinal_agente_emisor_secreto,
    )


def _require_agency(session: Session, agency_id: int) -> Agency:
    agency = session.get(Agency, agency_id)
    if agency is None or not agency.active:
        raise CredentialServiceError("Agencia no encontrada", status_code=404)
    return agency


def _company_by_slug(session: Session, slug: str) -> Company | None:
    return session.exec(select(Company).where(Company.slug == slug)).first()


def _find_credential(
    session: Session,
    agency_id: int,
    company_id: int,
) -> CompanyCredential | None:
    return session.exec(
        select(CompanyCredential).where(
            CompanyCredential.agency_id == agency_id,
            CompanyCredential.company_id == company_id,
        )
    ).first()


def _parse_payload(company_slug: str, payload: dict) -> BaseModel:
    model = CREDENTIAL_WRITES.get(company_slug)
    if model is None:
        raise CredentialServiceError(
            "Esta compañía todavía no tiene credenciales propias",
            status_code=404,
        )
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise CredentialServiceError(exc.errors()[0]["msg"], status_code=422) from exc


def _decrypt_payload(stored: str) -> dict[str, str]:
    plain = decrypt_for_admin(stored)
    if not plain:
        raise CredentialServiceError(
            "No se pudieron leer las credenciales. Revisá PASSWORD_ENCRYPTION_KEY.",
            status_code=500,
        )
    data = json.loads(plain)
    return {key: "" if value is None else str(value) for key, value in data.items()}
