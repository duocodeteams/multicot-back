from datetime import date
from typing import Annotated

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlmodel import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import get_current_user
from app.models import User
from app.models.company import Company
from app.models.sale import Sale
from app.sales.schemas import (
    CatalogItem,
    CatalogResponse,
    InterAssistQuoteSnapshot,
    NewTravelQuoteSnapshot,
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentResponse,
    PaymentSimulate,
    PaymentStart,
    SaleCreate,
    SaleDetailResponse,
    SaleListResponse,
    SaleResponse,
    TerrawindQuoteSnapshot,
    UniversalQuoteSnapshot,
)
from app.sales.service import (
    SaleFlowError,
    cancel_sale,
    confirm_inter_assist_from_notification,
    confirm_inter_assist_payment,
    confirm_omint_payment,
    confirm_omint_payment_from_notification,
    download_inter_assist_voucher_pdf,
    download_inter_assist_vouchers_zip,
    get_sale_for_user,
    list_countries,
    list_sales,
    list_inter_assist_catalog_countries,
    list_inter_assist_catalog_provinces,
    list_provinces,
    open_inter_assist_sale,
    open_omint_sale,
    sale_to_response,
    confirm_universal_payment,
    download_universal_voucher_pdf,
    open_universal_sale,
    set_inter_assist_passengers,
    set_omint_passengers,
    set_universal_passengers,
    simulate_universal_payment,
    start_universal_payment,
    simulate_inter_assist_payment,
    start_inter_assist_payment,
    start_omint_payment,
)
from app.sales.new_travel_flow import (
    confirm_new_travel_payment,
    list_countries as list_new_travel_countries,
    list_document_types as list_new_travel_document_types,
    list_provinces as list_new_travel_provinces,
    open_new_travel_sale,
    set_new_travel_passengers,
    start_new_travel_payment,
)
from app.sales.terrawind_flow import (
    confirm_terrawind_payment,
    list_terrawind_countries,
    list_terrawind_document_types,
    open_terrawind_sale,
    set_terrawind_passengers,
    start_terrawind_payment,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_OMINT = "omint"
_INTER_ASSIST = "inter_assist"
_UNIVERSAL = "universal"
_TERRAWIND = "terrawind"
_NEW_TRAVEL = "new_travel"


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
    """Abre una venta. Omint asigna producto; el resto congela el snapshot del viaje."""
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
            if not isinstance(data.quote, InterAssistQuoteSnapshot):
                raise SaleFlowError(400, "Inter Assist requiere el snapshot del viaje en `quote`")
            ia_quote = InterAssistQuoteSnapshot.model_validate(data.quote.model_dump())
            sale, created = open_inter_assist_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
                ia_quote,
            )
            company = _INTER_ASSIST
        elif slug == _UNIVERSAL:
            if not isinstance(data.quote, UniversalQuoteSnapshot):
                raise SaleFlowError(400, "Universal requiere el snapshot del viaje en `quote`")
            sale, created = open_universal_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
                data.quote,
            )
            company = _UNIVERSAL
        elif slug == _TERRAWIND:
            if data.quote is None:
                raise SaleFlowError(400, "Terrawind requiere el snapshot del viaje en `quote`")
            tw_quote = TerrawindQuoteSnapshot.model_validate(data.quote.model_dump())
            sale, created = open_terrawind_sale(
                session,
                current_user,
                data.company,
                data.quote_id,
                tw_quote,
            )
            company = _TERRAWIND
        elif slug in {_NEW_TRAVEL, "newtravel"}:
            if data.quote is None:
                raise SaleFlowError(400, "New Travel requiere el snapshot del viaje en `quote`")
            nt_quote = NewTravelQuoteSnapshot.model_validate(data.quote.model_dump())
            sale, created = open_new_travel_sale(
                session,
                current_user,
                _NEW_TRAVEL,
                data.quote_id,
                nt_quote,
            )
            company = _NEW_TRAVEL
        else:
            raise SaleFlowError(
                400,
                "La emisión para esta compañía todavía no está disponible",
            )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return sale_to_response(sale, company)


@router.get("", response_model=SaleListResponse)
def list_sales_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    emission_status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    company: str | None = Query(default=None, description="Slug de la compañía"),
    agency_id: int | None = Query(default=None),
    created_by_user_id: int | None = Query(
        default=None,
        description="Solo admin. Historial de ese usuario.",
    ),
    created_from: date | None = Query(default=None),
    created_to: date | None = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SaleListResponse:
    """Ventas del usuario del token. El admin ve todas. Sin pasajeros: el detalle es GET /v1/sales/{id}."""
    try:
        return list_sales(
            session,
            current_user,
            emission_status=emission_status,
            payment_status=payment_status,
            company=company,
            agency_id=agency_id,
            created_by_user_id=created_by_user_id,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


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


@router.get("/terrawind/countries", response_model=CatalogResponse)
def terrawind_countries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Países/destinos SETW. El `id` va en `quote.setw_country_id`."""
    _ = current_user
    try:
        items = list_terrawind_countries()
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(
        items=[CatalogItem(id=i["id"], name=i["name"]) for i in items]
    )


@router.get("/terrawind/document-types", response_model=CatalogResponse)
def terrawind_document_types(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Tipos de documento SETW (referencia). En pasajeros usamos dni/passport/cuit."""
    _ = current_user
    try:
        items = list_terrawind_document_types()
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(
        items=[CatalogItem(id=i["id"], name=i["name"]) for i in items]
    )


@router.get("/new_travel/countries", response_model=CatalogResponse)
def new_travel_countries(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Países New Travel. El `id` (ISO) va en `country_id` del titular/pasajeros."""
    _ = current_user
    try:
        items = list_new_travel_countries()
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return CatalogResponse(
        items=[CatalogItem(id=i["id"], name=i["name"]) for i in items]
    )


@router.get(
    "/new_travel/countries/{country_id}/provinces",
    response_model=CatalogResponse,
)
def new_travel_provinces(
    country_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Provincias locales (Georef si AR). No se envían a New Travel; vacío en otros países."""
    _ = current_user
    items = list_new_travel_provinces(country_id)
    return CatalogResponse(
        items=[CatalogItem(id=i["id"], name=i["name"]) for i in items]
    )


@router.get("/new_travel/document-types", response_model=CatalogResponse)
def new_travel_document_types(
    current_user: Annotated[User, Depends(get_current_user)],
) -> CatalogResponse:
    """Tipos de documento para el form: passport/dni/cuit (el backend mapea a New Travel)."""
    _ = current_user
    items = list_new_travel_document_types()
    return CatalogResponse(
        items=[CatalogItem(id=i["id"], name=i["name"]) for i in items]
    )


@router.post("/{sale_id}/cancel", response_model=SaleDetailResponse)
def cancel_sale_route(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SaleDetailResponse:
    """Cancela la venta si el pago no está pendiente ni aprobado. La cotización queda tomada."""
    try:
        return cancel_sale(session, current_user, sale_id)
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


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
    voucher_id: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """Proxy del PDF. Inter Assist acepta `voucher_id`. Universal también. El front lo pide con el JWT."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _INTER_ASSIST:
            content, filename = download_inter_assist_voucher_pdf(
                session, current_user, sale_id, lang=lang, voucher_id=voucher_id
            )
        elif slug == _UNIVERSAL:
            content, filename = download_universal_voucher_pdf(
                session, current_user, sale_id, lang=lang
            )
        else:
            raise SaleFlowError(400, "La descarga de PDF por proxy no aplica a esta compañía")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.get("/{sale_id}/vouchers/zip")
def download_sale_vouchers_zip(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    lang: Annotated[str, Query(pattern="^(es|en|pt)$")] = "es",
) -> Response:
    """Zip con todos los PDFs de la venta (un voucher por pasajero)."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug != _INTER_ASSIST:
            raise SaleFlowError(
                400,
                "La descarga de vouchers en zip solo aplica a Inter Assist",
            )
        content, filename = download_inter_assist_vouchers_zip(
            session, current_user, sale_id, lang=lang
        )
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
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
        if slug == _UNIVERSAL:
            return set_universal_passengers(session, current_user, sale_id, data)
        if slug == _TERRAWIND:
            return set_terrawind_passengers(session, current_user, sale_id, data)
        if slug == _NEW_TRAVEL:
            return set_new_travel_passengers(session, current_user, sale_id, data)
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
    """Abre el cobro. Omint usa su gateway; Inter Assist, Mercado Pago o Payway; Universal simula; Terrawind usa Nave."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _OMINT:
            return start_omint_payment(session, current_user, sale_id, data)
        if slug == _INTER_ASSIST:
            data = data.model_copy(update=_inter_assist_return_urls(sale_id))
            return start_inter_assist_payment(session, current_user, sale_id, data)
        if slug == _UNIVERSAL:
            return start_universal_payment(session, current_user, sale_id, data)
        if slug == _TERRAWIND:
            return start_terrawind_payment(session, current_user, sale_id, data)
        if slug == _NEW_TRAVEL:
            return start_new_travel_payment(session, current_user, sale_id, data)
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
    """Mueve el pago simulado de Inter Assist o Universal a pending, approved o rejected."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _INTER_ASSIST:
            return simulate_inter_assist_payment(session, current_user, sale_id, data)
        if slug == _UNIVERSAL:
            return simulate_universal_payment(session, current_user, sale_id, data)
        raise SaleFlowError(400, "La simulación de pago no aplica a esta compañía")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{sale_id}/payment/confirm", response_model=PaymentConfirmResponse)
def confirm_payment(
    sale_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PaymentConfirmResponse:
    """Confirma el pago y, si ya está aprobado, deja el voucher de la compañía."""
    try:
        slug = _sale_company_slug(session, sale_id)
        if slug == _OMINT:
            return confirm_omint_payment(session, current_user, sale_id)
        if slug == _INTER_ASSIST:
            return confirm_inter_assist_payment(session, current_user, sale_id)
        if slug == _UNIVERSAL:
            return confirm_universal_payment(session, current_user, sale_id)
        if slug == _TERRAWIND:
            return confirm_terrawind_payment(session, current_user, sale_id)
        if slug == _NEW_TRAVEL:
            return confirm_new_travel_payment(session, current_user, sale_id)
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    except SaleFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post(
    "/{sale_id}/interassist/payment-notification",
    response_model=PaymentConfirmResponse,
)
async def interassist_payment_notification(
    sale_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> PaymentConfirmResponse:
    """Aviso de Inter Assist cuando el pago quedó aprobado y la venta emitida.

    No exige login: lo llama el servidor de Inter Assist. El estado se confirma
    con GET /api/ventas/checkout/{uuid}; el body no alcanza para emitir.
    """
    raw = await request.body()
    logger.info(
        "interassist payment notification sale_id=%s bytes=%s",
        sale_id,
        len(raw),
    )
    try:
        return confirm_inter_assist_from_notification(session, sale_id)
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


def _inter_assist_return_urls(sale_id: int) -> dict[str, str]:
    """El checkout de Inter vuelve al portal. El front no manda estas URLs."""
    origin = get_settings().portal_base_url.strip().rstrip("/") or "https://biantravel.com"
    base = f"{origin}/portal/pago?sale_id={sale_id}"
    return {
        "return_url_ok": f"{base}&payment=ok",
        "return_url_error": f"{base}&payment=error",
        "return_url_pending": f"{base}&payment=pending",
    }


def _sale_company_slug(session: Session, sale_id: int) -> str:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    company = session.get(Company, sale.company_id)
    if company is None:
        raise SaleFlowError(400, "La compañía de la venta no existe")
    return company.slug
