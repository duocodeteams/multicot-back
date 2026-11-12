from typing import Annotated

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_user
from app.models import User
from app.models.company import Company
from app.models.sale import Sale
from app.sales.schemas import (
    CatalogResponse,
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentResponse,
    PaymentSimulate,
    PaymentStart,
    SaleCreate,
    SaleDetailResponse,
    SaleResponse,
)
from app.sales.service import (
    SaleFlowError,
    confirm_inter_assist_payment,
    confirm_omint_payment,
    confirm_omint_payment_from_notification,
    confirm_cardinal_payment,
    download_inter_assist_voucher_pdf,
    get_sale_for_user,
    list_cardinal_catalog,
    list_countries,
    list_inter_assist_catalog_countries,
    list_inter_assist_catalog_provinces,
    list_provinces,
    open_cardinal_sale,
    open_inter_assist_sale,
    open_omint_sale,
    sale_to_response,
    set_cardinal_passengers,
    set_inter_assist_passengers,
    set_omint_passengers,
    simulate_cardinal_payment,
    simulate_inter_assist_payment,
    start_cardinal_payment,
    start_inter_assist_payment,
    start_omint_payment,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_OMINT = "omint"
_INTER_ASSIST = "inter_assist"
_CARDINAL = "cardinal"


def _company_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


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
    """Abre una venta. Omint asigna el producto, Cardinal congela costoFinal e Inter Assist el snapshot local."""
    slug = _company_slug(data.company)
    try:
        if slug == _OMINT:
            sale, created = open_omint_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
            )
            company = _OMINT
        elif slug == _INTER_ASSIST:
            if data.quote is None:
                raise SaleFlowError(400, "Inter Assist requiere el snapshot del viaje en `quote`")
            sale, created = open_inter_assist_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
                data.quote,
            )
            company = _INTER_ASSIST
        elif slug == _CARDINAL:
            sale, created = open_cardinal_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
            )
            company = _CARDINAL
        else:
            raise SaleFlowError(
                400,
                "La emisión para esta compañía todavía no está disponible",
            )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return sale_to_response(sale, company)


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


@router.get("/inter_assist/countries", response_model=CatalogResponse)
def inter_assist_countries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Países para el titular Inter Assist (REST Countries). El id (cca2) va en country_id."""
    _ = current_user
    try:
        items = list_inter_assist_catalog_countries()
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(items=items)


@router.get(
    "/inter_assist/countries/{country_id}/provinces",
    response_model=CatalogResponse,
)
def inter_assist_provinces(
    country_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Provincias AR (Georef). Otros países: lista vacía."""
    _ = current_user
    try:
        items = list_inter_assist_catalog_provinces(country_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(items=items)


@router.get("/cardinal/document-types", response_model=CatalogResponse)
def cardinal_document_types(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Tipos de documento de Cardinal. El pasajero sigue mandando dni, passport o cuit."""
    try:
        items = list_cardinal_catalog(session, current_user, "tiposDocumentos")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(items=items)


@router.get("/cardinal/countries", response_model=CatalogResponse)
def cardinal_countries(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Países de residencia de Cardinal. El id numérico va en country_id de cada pasajero."""
    try:
        items = list_cardinal_catalog(session, current_user, "paisesResidencia")
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
        return get_sale_for_user(session, current_user, sale_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/{sale_id}/voucher/pdf")
def download_sale_voucher_pdf(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    lang: Annotated[str, Query(pattern="^(es|en|pt)$")] = "es",
) -> Response:
    """Proxy del PDF de Inter Assist. El front debe pedirlo con el JWT (no abrir la URL externa)."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug != _INTER_ASSIST:
            raise SaleFlowError(
                400,
                "La descarga de PDF por proxy solo aplica a Inter Assist",
            )
        content, filename = download_inter_assist_voucher_pdf(
            session, current_user, sale_id, lang=lang
        )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.post("/{sale_id}/passengers", response_model=PassengersResponse)
def set_passengers(
    sale_id: int,
    data: PassengersCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassengersResponse:
    """Carga titular, pasajeros y contacto de emergencia."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _OMINT:
            return set_omint_passengers(session, current_user, sale_id, data)
        if slug == _INTER_ASSIST:
            return set_inter_assist_passengers(session, current_user, sale_id, data)
        if slug == _CARDINAL:
            return set_cardinal_passengers(session, current_user, sale_id, data)
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment", response_model=PaymentResponse)
def start_payment(
    sale_id: int,
    data: PaymentStart,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentResponse:
    """Abre el cobro. Omint usa su gateway; Inter Assist usa la pasarela simulada."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _OMINT:
            return start_omint_payment(session, current_user, sale_id, data)
        if slug == _INTER_ASSIST:
            return start_inter_assist_payment(session, current_user, sale_id, data)
        if slug == _CARDINAL:
            return start_cardinal_payment(session, current_user, sale_id, data)
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment/simulate", response_model=PaymentResponse)
def simulate_payment(
    sale_id: int,
    data: PaymentSimulate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentResponse:
    """Solo Inter Assist: mueve el pago simulado a pending, approved o rejected."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _INTER_ASSIST:
            return simulate_inter_assist_payment(session, current_user, sale_id, data)
        if slug == _CARDINAL:
            return simulate_cardinal_payment(session, current_user, sale_id, data)
        raise SaleFlowError(400, "La simulación de pago solo aplica a Inter Assist y Cardinal")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment/confirm", response_model=PaymentConfirmResponse)
def confirm_payment(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentConfirmResponse:
    """Confirma el pago. En Inter Assist, si está approved emite el voucher."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _OMINT:
            return confirm_omint_payment(session, current_user, sale_id)
        if slug == _INTER_ASSIST:
            return confirm_inter_assist_payment(session, current_user, sale_id)
        if slug == _CARDINAL:
            return confirm_cardinal_payment(session, current_user, sale_id)
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
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


def _sale_company_slug(session: Session, sale_id: int) -> str:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    company = session.get(Company, sale.company_id)
    if company is None:
        raise SaleFlowError(400, "La compañía de la venta no existe")
    return company.slug
