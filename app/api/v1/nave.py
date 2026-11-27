"""Endpoints Nave Checkout: crear link de pago y recibir webhooks."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_user
from app.models import User
from app.nave.schemas import (
    NaveBuyer,
    NavePaymentLinkCreate,
    NavePaymentLinkResponse,
    NaveWebhookPayload,
    NaveWebhookResponse,
)
from app.sales.terrawind_flow import handle_nave_payment_notification
from app.services.nave import (
    NaveError,
    create_payment_request,
    extract_nave_status,
    get_payment_by_check_url,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/payment-links",
    response_model=NavePaymentLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_nave_payment_link(
    data: NavePaymentLinkCreate,
    current_user: Annotated[User, Depends(get_current_user)],
) -> NavePaymentLinkResponse:
    """Crea una intención de pago en Nave y devuelve el checkout_url para el pax."""
    external_id = (data.external_payment_id or _new_external_id()).strip()[:36]
    buyer_payload = _buyer_dict(data.buyer) if data.buyer else None
    try:
        link = create_payment_request(
            external_payment_id=external_id,
            amount=data.amount,
            product_name=data.product_name.strip(),
            product_description=(
                data.product_description.strip() if data.product_description else None
            ),
            buyer=buyer_payload,
            callback_url=data.callback_url,
            duration_time=data.duration_time,
        )
    except NaveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    logger.info(
        "Nave: link creado user_id=%s external_payment_id=%s payment_request_id=%s",
        current_user.id,
        link.external_payment_id,
        link.payment_request_id,
    )
    return NavePaymentLinkResponse(
        external_payment_id=link.external_payment_id,
        payment_request_id=link.payment_request_id,
        checkout_url=link.checkout_url,
    )


@router.post("/webhooks", response_model=NaveWebhookResponse)
def nave_webhook(
    payload: NaveWebhookPayload,
    session: Annotated[Session, Depends(get_session)],
) -> NaveWebhookResponse:
    """Notificación de Nave. Si el pago es de una venta Terrawind, actualiza e intenta emitir."""
    logger.info(
        "Nave webhook: payment_id=%s external_payment_id=%s check_url=%s",
        payload.payment_id,
        payload.external_payment_id,
        payload.payment_check_url,
    )

    payment_status: str | None = None
    try:
        remote = get_payment_by_check_url(payload.payment_check_url)
        payment_status = extract_nave_status(remote)
        logger.info(
            "Nave webhook: estado consultado payment_id=%s status=%s",
            payload.payment_id,
            payment_status,
        )
    except NaveError as exc:
        logger.error(
            "Nave webhook: no se pudo consultar payment_check_url payment_id=%s detail=%s",
            payload.payment_id,
            exc.detail,
        )

    try:
        handle_nave_payment_notification(
            session,
            external_payment_id=payload.external_payment_id,
            payment_id=payload.payment_id,
            payment_check_url=payload.payment_check_url,
        )
    except Exception:
        logger.exception(
            "Nave webhook: error procesando venta external_payment_id=%s",
            payload.external_payment_id,
        )

    return NaveWebhookResponse(
        received=True,
        payment_id=payload.payment_id,
        external_payment_id=payload.external_payment_id,
        payment_status=payment_status,
    )


def _new_external_id() -> str:
    return uuid.uuid4().hex[:32]


def _buyer_dict(buyer: NaveBuyer) -> dict[str, Any]:
    payload = buyer.model_dump(exclude_none=True)
    address = payload.get("billing_address")
    if isinstance(address, dict):
        cleaned = {k: v for k, v in address.items() if v is not None}
        if cleaned:
            payload["billing_address"] = cleaned
        else:
            payload.pop("billing_address", None)
    return payload
