from typing import Annotated

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_user
from app.models import User
from app.sales.schemas import (
    CatalogResponse,
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentResponse,
    PaymentStart,
    SaleCreate,
    SaleDetailResponse,
    SaleResponse,
)
from app.sales.service import (
    SaleFlowError,
    confirm_omint_payment,
    confirm_omint_payment_from_notification,
    list_countries,
    list_provinces,
    get_omint_sale,
    open_omint_sale,
    sale_to_response,
    set_omint_passengers,
    start_omint_payment,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _notification_fields(raw: bytes) -> tuple[object, object]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return payload.get("IdStatus"), payload.get("Descripcion")


@router.post("", response_model=SaleResponse)
def create_sale(
    data: SaleCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SaleResponse:
    """Elige un plan de Omint, lo asigna en su API y abre la venta con ese precio."""
    try:
        sale, created = open_omint_sale(
            session,
            current_user,
            data.company,
            data.quote_id,
        )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return sale_to_response(sale, "omint")


@router.get("/omint/countries", response_model=CatalogResponse)
def omint_countries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Países habilitados para el titular. El id es el que después se manda en el pasajero."""
    _ = current_user
    try:
        items = list_countries()
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(items=items)


@router.get("/omint/countries/{country_id}/provinces", response_model=CatalogResponse)
def omint_provinces(
    country_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    _ = current_user
    try:
        items = list_provinces(country_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(items=items)


@router.get("/{sale_id}", response_model=SaleDetailResponse)
def get_sale(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SaleDetailResponse:
    """Estado de la venta para retomar: pasajeros, medios de pago, checkout y voucher."""
    try:
        return get_omint_sale(session, current_user, sale_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/passengers", response_model=PassengersResponse)
def set_passengers(
    sale_id: int,
    data: PassengersCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassengersResponse:
    """Carga el titular, los pasajeros y el contacto de emergencia en Omint."""
    try:
        return set_omint_passengers(session, current_user, sale_id, data)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment", response_model=PaymentResponse)
def start_payment(
    sale_id: int,
    data: PaymentStart,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentResponse:
    """Abre el gateway de Omint. Si ya hay un checkout pendiente, devuelve esa URL."""
    try:
        return start_omint_payment(session, current_user, sale_id, data)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment/confirm", response_model=PaymentConfirmResponse)
def confirm_payment(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentConfirmResponse:
    """Una consulta a ConfirmPayment. El panel puede repetirla: no abre otro cobro."""
    try:
        return confirm_omint_payment(session, current_user, sale_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/omint/payment-notification", response_model=PaymentConfirmResponse)
async def omint_payment_notification(
    sale_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> PaymentConfirmResponse:
    """Aviso de Mercado Pago. No exige login: lo llama el servidor de Omint.

    El body trae IdStatus, Descripcion y Message, sin firma ni id de cotización.
    La venta sale de la URL. No se guarda el cuerpo. El voucher lo dice ConfirmPayment.
    Con tarjeta este aviso no llega: el panel confirma al volver de la pasarela.
    """
    raw = await request.body()
    status_id, description = _notification_fields(raw)
    logger.info(
        "omint payment notification sale_id=%s bytes=%s id_status=%s descripcion=%s",
        sale_id,
        len(raw),
        status_id,
        description,
    )
    try:
        return confirm_omint_payment_from_notification(session, sale_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
