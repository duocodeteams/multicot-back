"""Abre una venta de Omint al elegir un plan y asigna el producto en su API."""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import get_settings
from app.credentials.service import resolve_universal_organization
from app.models.company import Company
from app.models.plan import Plan
from app.models.sale import (
    EmissionAttempt,
    EmissionStatus,
    Passenger,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Sale,
    SalePaymentStatus,
    Voucher,
    VoucherStatus,
)
from app.models.seller import Seller
from app.models.user import User, UserRole
from app.quotations.schemas import QuoteRequest
from app.sales.schemas import (
    EmergencyContactResponse,
    FinancingOption,
    InterAssistQuoteSnapshot,
    PassengerInput,
    PassengerResponse,
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentMethodOption,
    PaymentResponse,
    PaywayCardOption,
    PaymentSimulate,
    PaymentStart,
    SaleDetailResponse,
    SaleListResponse,
    SaleResponse,
    SaleVoucherItem,
    UniversalQuoteSnapshot,
)
from app.services.companies.inter_assist import InterAssistQuoteProvider
from app.services.companies.inter_assist_catalog import (
    list_inter_assist_countries,
    list_inter_assist_provinces,
)
from app.services.companies.universal_emission import (
    UniversalEmissionError,
    UniversalPassengerDraft,
    emit_universal_voucher,
    fetch_universal_voucher_pdf,
    retire_universal_lead,
    universal_charge,
)
from app.services.companies.inter_assist_emission import (
    InterAssistCheckoutResult,
    InterAssistClientDraft,
    InterAssistEmissionError,
    InterAssistVoucherInfo,
    PaywayCardsResult,
    create_inter_assist_checkout,
    emit_inter_assist_sale,
    emission_type_and_periodo,
    fetch_voucher_pdf,
    get_inter_assist_checkout,
    get_inter_assist_venta_vouchers,
    list_payway_cards,
)
from app.services.companies.omint_emission import (
    DOCUMENT_TYPE_IDS,
    FINANCING_REQUIRED_CODES,
    PAYMENT_METHOD_NAMES,
    OmintAssignFailed,
    OmintEmergencyDraft,
    OmintEmissionError,
    OmintPassengerDraft,
    OmintSelection,
    assign_omint_passengers,
    confirm_omint_checkout,
    list_omint_countries,
    list_omint_provinces,
    select_omint_product,
    start_omint_checkout,
)

logger = logging.getLogger(__name__)

_OMINT_SLUG = "omint"
_INTER_ASSIST_SLUG = "inter_assist"
_UNIVERSAL_SLUG = "universal"
_SIMULATED_PAYMENT_METHOD = {
    "id": "simulated",
    "code": "SIMULATED",
    "financing": [],
}
_LOCKED_STATUSES = {
    EmissionStatus.PASSENGERS_COMPLETE.value,
    EmissionStatus.EMITTING.value,
    EmissionStatus.ISSUED.value,
    EmissionStatus.CANCELLED.value,
}
_AMOUNT_TOLERANCE = Decimal("0.02")


class SaleFlowError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def open_omint_sale(
    session: Session,
    user: User,
    company_name: str,
    quote_id: str,
) -> tuple[Sale, bool]:
    """Crea la venta o devuelve la ya abierta para esa cotización.

    El segundo valor indica si la fila se creó en esta llamada.
    """
    if company_name.strip().lower() != _OMINT_SLUG:
        raise SaleFlowError(
            400,
            "La emisión para esta compañía todavía no está disponible",
        )
    quotation_id, product_id = _parse_quote_id(quote_id)
    company = session.exec(select(Company).where(Company.slug == _OMINT_SLUG)).first()
    if company is None or company.id is None:
        raise SaleFlowError(400, "Omint no está cargada como compañía")
    if not company.active:
        raise SaleFlowError(400, "Omint no está activa")

    reference = f"omint:{quotation_id.lower()}"
    existing = session.exec(
        select(Sale).where(Sale.provider_reference == reference)
    ).first()
    if existing is not None:
        if existing.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta")
        if existing.emission_status in _LOCKED_STATUSES:
            raise SaleFlowError(409, "Esa venta ya no se puede volver a cotizar")
        same_product = _same_id(
            (existing.provider_context or {}).get("quotation_product_id"),
            product_id,
        )
        if (
            existing.emission_status == EmissionStatus.PRODUCT_SELECTED.value
            and same_product
        ):
            return existing, False

    try:
        selection = select_omint_product(quotation_id, product_id)
    except OmintAssignFailed as exc:
        sale = _upsert_sale(
            session,
            user=user,
            company=company,
            reference=reference,
            existing=existing,
            selection=exc.selection,
            emission_status=EmissionStatus.EMISSION_FAILED.value,
        )
        _add_attempt(
            session,
            sale,
            success=False,
            http_status=exc.http_status,
            request_payload=exc.selection.assign_request,
            response_payload=exc.response_body,
            error_message=exc.detail,
        )
        _commit(session)
        logger.info(
            "sale omint assign failed sale_id=%s quotation=%s",
            sale.id,
            quotation_id,
        )
        raise SaleFlowError(502, exc.detail) from exc
    except OmintEmissionError as exc:
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    sale = _upsert_sale(
        session,
        user=user,
        company=company,
        reference=reference,
        existing=existing,
        selection=selection,
        emission_status=EmissionStatus.PRODUCT_SELECTED.value,
    )
    _add_attempt(
        session,
        sale,
        success=True,
        http_status=selection.assign_status_code,
        request_payload=selection.assign_request,
        response_payload=selection.assign_response,
        error_message=None,
    )
    _commit(session)
    logger.info(
        "sale omint product_selected sale_id=%s plan=%s amount=%s %s",
        sale.id,
        sale.external_plan_id,
        sale.amount,
        sale.currency,
    )
    return sale, existing is None


def set_omint_passengers(
    session: Session,
    user: User,
    sale_id: int,
    data: PassengersCreate,
) -> PassengersResponse:
    """Valida edades contra la cotización y carga los pasajeros en Omint."""
    sale = _editable_omint_sale(session, user, sale_id)
    if sale.departure_date is None:
        raise SaleFlowError(400, "La venta no tiene fecha de salida")
    quoted_ages = [int(age) for age in (sale.quoted_ages or [])]
    if len(data.passengers) != len(quoted_ages):
        raise SaleFlowError(
            400,
            "La cantidad de pasajeros tiene que coincidir con la cotización",
        )
    ages = [_age_on(passenger.birth_date, sale.departure_date) for passenger in data.passengers]
    if Counter(ages) != Counter(quoted_ages):
        raise SaleFlowError(
            400,
            "Las edades a la fecha de salida no coinciden con la cotización",
        )
    quotation_id = str((sale.provider_context or {}).get("quotation_id") or "").strip()
    if not quotation_id:
        raise SaleFlowError(400, "La venta no tiene la cotización de Omint")

    drafts = [_draft(passenger) for passenger in data.passengers]
    emergency = OmintEmergencyDraft(
        first_name=data.emergency_contact.first_name.strip(),
        last_name=data.emergency_contact.last_name.strip(),
        phone=data.emergency_contact.phone.strip(),
        email=str(data.emergency_contact.email) if data.emergency_contact.email else None,
    )
    try:
        result = assign_omint_passengers(
            quotation_id,
            drafts,
            emergency,
            receive_promotions=data.receive_promotions,
        )
    except OmintEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="assign_passengers",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    _replace_passengers(session, sale, data)
    sale.emergency_first_name = emergency.first_name
    sale.emergency_last_name = emergency.last_name
    sale.emergency_phone = emergency.phone
    sale.emergency_email = emergency.email
    sale.emission_status = EmissionStatus.PASSENGERS_COMPLETE.value
    sale.updated_at = datetime.utcnow()
    context = dict(sale.provider_context or {})
    context["receive_promotions"] = data.receive_promotions
    context["payment_methods"] = result.payment_methods
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="assign_passengers",
        success=True,
        http_status=result.status_code,
        request_payload=result.request_body,
        response_payload={"paymentMethods": result.payment_methods},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    logger.info("sale omint passengers_complete sale_id=%s passengers=%s", sale.id, len(data.passengers))
    return passengers_response(session, sale)


def start_omint_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentStart,
) -> PaymentResponse:
    """Abre el checkout de Omint, o devuelve el que ya está pendiente."""
    sale = _payable_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    existing = _pending_payment(session, sale.id or 0)
    if existing is not None and existing.checkout_url:
        return _payment_response(sale, existing.checkout_url, created=False)

    method = _payment_method(sale, data.payment_method_id)
    code = str(method.get("code") or "").strip().upper()
    financing_id = (data.financing_id or "").strip() or None
    if code in FINANCING_REQUIRED_CODES and not financing_id:
        raise SaleFlowError(400, "Ese medio de pago exige una financiación")
    if financing_id and not _financing_belongs(method, financing_id):
        raise SaleFlowError(400, "La financiación no corresponde a ese medio de pago")

    quotation_id = str((sale.provider_context or {}).get("quotation_id") or "").strip()
    if not quotation_id:
        raise SaleFlowError(400, "La venta no tiene la cotización de Omint")
    notification_url = _notification_url(sale.id or 0)
    try:
        checkout = start_omint_checkout(
            quotation_id,
            data.payment_method_id.strip(),
            financing_id,
            url_ok=data.return_url_ok.strip(),
            url_error=data.return_url_error.strip(),
            url_pending=data.return_url_pending.strip(),
            url_notification=notification_url,
        )
    except OmintEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="process_payment",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    payment = Payment(
        sale_id=sale.id or 0,
        provider=PaymentProvider.OMINT_GATEWAY.value,
        status=PaymentStatus.PENDING.value,
        amount=sale.amount,
        currency=sale.currency,
        checkout_url=checkout.payment_url[:2000],
        external_id=data.payment_method_id.strip()[:80],
        financing_id=financing_id[:80] if financing_id else None,
        provider_data={"payment_method_code": code},
    )
    session.add(payment)
    sale.payment_status = SalePaymentStatus.PENDING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="process_payment",
        success=True,
        http_status=checkout.status_code,
        request_payload=checkout.request_body,
        response_payload={"paymentUrl": checkout.payment_url},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, checkout.payment_url, created=True)


def confirm_omint_payment(
    session: Session,
    user: User,
    sale_id: int,
) -> PaymentConfirmResponse:
    """Una consulta a ConfirmPayment. No abre otro pago si todavía no está acreditado."""
    sale = _owned_omint_sale(session, user, sale_id)
    return _confirm_sale(session, sale)


def confirm_omint_payment_from_notification(
    session: Session,
    sale_id: int,
) -> PaymentConfirmResponse:
    """Aviso de Mercado Pago. El estado real lo dice ConfirmPayment, no el body."""
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _OMINT_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Omint")
    if (
        sale.payment_status != SalePaymentStatus.PENDING.value
        and sale.emission_status != EmissionStatus.ISSUED.value
    ):
        return _confirm_response(sale, False, None)
    return _confirm_sale(session, sale)


def list_countries() -> list[dict[str, str]]:
    try:
        return list_omint_countries()
    except OmintEmissionError as exc:
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc


def list_provinces(country_id: str) -> list[dict[str, str]]:
    if not country_id.strip() or "/" in country_id:
        raise SaleFlowError(400, "El país no es válido")
    try:
        return list_omint_provinces(country_id.strip())
    except OmintEmissionError as exc:
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc


def list_inter_assist_catalog_countries() -> list[dict[str, str]]:
    """Países para el formulario Inter Assist (REST Countries / fallback)."""
    return list_inter_assist_countries()


def list_inter_assist_catalog_provinces(country_id: str) -> list[dict[str, str]]:
    """Provincias AR vía Georef. Otros países: lista vacía."""
    if not country_id.strip() or "/" in country_id:
        raise SaleFlowError(400, "El país no es válido")
    return list_inter_assist_provinces(country_id.strip())


def get_omint_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    """Devuelve la venta del usuario para retomar la emisión."""
    sale = _owned_omint_sale(session, user, sale_id, forbidden="No podés ver esta venta")
    return sale_detail(session, sale, _OMINT_SLUG)


def open_inter_assist_sale(
    session: Session,
    user: User,
    company_name: str,
    quote_id: str,
    quote: InterAssistQuoteSnapshot,
) -> tuple[Sale, bool]:
    """Abre la venta local congelando el plan cotizado. No llama a Inter Assist todavía."""
    if _normalize_company(company_name) != _INTER_ASSIST_SLUG:
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    plan_id = quote_id.strip()
    if not plan_id or "/" in plan_id:
        raise SaleFlowError(400, "El plan de Inter Assist no es válido")
    company = session.exec(select(Company).where(Company.slug == _INTER_ASSIST_SLUG)).first()
    if company is None or company.id is None:
        raise SaleFlowError(400, "Inter Assist no está cargada como compañía")
    if not company.active:
        raise SaleFlowError(400, "Inter Assist no está activa")

    try:
        emission_type_and_periodo(quote.trip_type, quote.days_range)
    except InterAssistEmissionError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc
    if quote.trip_type != "multiviaje" and quote.return_date is None:
        raise SaleFlowError(400, "Falta la fecha de regreso")

    _validate_inter_assist_quote_amount(plan_id, quote)

    # Doc Inter §5.4: promocion_id explícito; 0 = forzar sin promo. No auto-resolver.
    promocion_id = int(quote.promocion_id or 0)
    reference = _inter_assist_reference(user.id or 0, plan_id, quote)
    existing = session.exec(select(Sale).where(Sale.provider_reference == reference)).first()
    if existing is not None:
        if existing.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta")
        # Mismo usuario: devolver la venta para retomar (también si ya avanzó).
        if existing.emission_status in _LOCKED_STATUSES:
            return existing, False
        if existing.emission_status == EmissionStatus.PRODUCT_SELECTED.value:
            return existing, False

    context = {
        "plan_id": plan_id,
        "promocion_id": promocion_id,
        "trip_type": quote.trip_type,
        "amount": str(quote.amount),
        "currency": quote.currency.upper(),
    }
    local_plan_id = _local_plan_id(session, company.id, plan_id)
    if existing is None:
        seller_id, agency_id = _attribution(session, user)
        sale = Sale(
            created_by_user_id=user.id or 0,
            agency_id=agency_id,
            seller_id=seller_id,
            company_id=company.id,
            emission_status=EmissionStatus.PRODUCT_SELECTED.value,
            payment_status=SalePaymentStatus.NOT_STARTED.value,
            provider_reference=reference,
            origin=quote.origin[:16],
            destination_id=quote.destination_id,
            trip_type=quote.trip_type,
            days_range=quote.days_range,
            departure_date=quote.departure_date,
            return_date=quote.return_date,
            quoted_ages=list(quote.ages),
            external_plan_id=plan_id[:80],
            plan_name=quote.plan_name[:200],
            plan_id=local_plan_id,
            currency=quote.currency.upper()[:3],
            amount=quote.amount,
            net_amount=quote.net_amount,
            base_amount=quote.base_amount,
            markup=quote.markup,
            provider_context=context,
        )
        session.add(sale)
        created = True
    else:
        existing.emission_status = EmissionStatus.PRODUCT_SELECTED.value
        existing.origin = quote.origin[:16]
        existing.destination_id = quote.destination_id
        existing.trip_type = quote.trip_type
        existing.days_range = quote.days_range
        existing.departure_date = quote.departure_date
        existing.return_date = quote.return_date
        existing.quoted_ages = list(quote.ages)
        existing.external_plan_id = plan_id[:80]
        existing.plan_name = quote.plan_name[:200]
        existing.plan_id = local_plan_id
        existing.currency = quote.currency.upper()[:3]
        existing.amount = quote.amount
        existing.net_amount = quote.net_amount
        existing.base_amount = quote.base_amount
        existing.markup = quote.markup
        existing.provider_context = context
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        sale = existing
        created = False

    # Igual que Omint: hace falta el id real antes del EmissionAttempt (FK).
    session.flush()
    _add_attempt(
        session,
        sale,
        success=True,
        http_status=200,
        request_payload={"plan_id": plan_id, "quote": quote.model_dump(mode="json")},
        response_payload={"provider_reference": reference},
        error_message=None,
        step="select_product",
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raced = session.exec(
            select(Sale).where(Sale.provider_reference == reference)
        ).first()
        if raced is None or raced.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta") from exc
        return raced, False
    session.refresh(sale)
    logger.info(
        "sale inter_assist product_selected sale_id=%s plan=%s amount=%s %s",
        sale.id,
        sale.external_plan_id,
        sale.amount,
        sale.currency,
    )
    return sale, created


def set_inter_assist_passengers(
    session: Session,
    user: User,
    sale_id: int,
    data: PassengersCreate,
) -> PassengersResponse:
    """Guarda pasajeros en local. Inter Assist recibe los clientes recién al emitir."""
    sale = _editable_inter_assist_sale(session, user, sale_id)
    if sale.departure_date is None:
        raise SaleFlowError(400, "La venta no tiene fecha de salida")
    quoted_ages = [int(age) for age in (sale.quoted_ages or [])]
    if len(data.passengers) != len(quoted_ages):
        raise SaleFlowError(
            400,
            "La cantidad de pasajeros tiene que coincidir con la cotización",
        )
    ages = [_age_on(passenger.birth_date, sale.departure_date) for passenger in data.passengers]
    if Counter(ages) != Counter(quoted_ages):
        raise SaleFlowError(
            400,
            "Las edades a la fecha de salida no coinciden con la cotización",
        )

    _replace_passengers_generic(session, sale, data)
    sale.emergency_first_name = data.emergency_contact.first_name.strip()
    sale.emergency_last_name = data.emergency_contact.last_name.strip()
    sale.emergency_phone = data.emergency_contact.phone.strip()
    sale.emergency_email = (
        str(data.emergency_contact.email) if data.emergency_contact.email else None
    )
    sale.emission_status = EmissionStatus.PASSENGERS_COMPLETE.value
    sale.updated_at = datetime.utcnow()
    catalog = _load_payway_catalog()
    payment_methods = _inter_assist_payment_methods(catalog)
    context = dict(sale.provider_context or {})
    context["receive_promotions"] = data.receive_promotions
    context["payment_methods"] = payment_methods
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="assign_passengers",
        success=True,
        http_status=200,
        request_payload={"passengers": len(data.passengers)},
        response_payload={"payment_methods": payment_methods},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    logger.info(
        "sale inter_assist passengers_complete sale_id=%s passengers=%s",
        sale.id,
        len(data.passengers),
    )
    return passengers_response(session, sale)


def start_inter_assist_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentStart,
) -> PaymentResponse:
    """Abre el checkout de Mercado Pago o Payway en Inter Assist."""
    sale = _payable_inter_assist_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")

    gateway = _inter_assist_gateway(data.payment_method_id)
    if gateway == PaymentProvider.PAYWAY.value and _local_return_host(data.return_url_ok):
        raise SaleFlowError(
            400,
            "Payway no acepta volver a localhost. Definí PORTAL_BASE_URL con un https público y reintentá",
        )
    card_id, installments = _payway_selection(sale, data, gateway)
    existing = _pending_payment(session, sale.id or 0)
    if existing is not None and _reusable_inter_assist_checkout(
        existing, gateway, card_id, installments, data
    ):
        return _payment_response(
            sale,
            existing.checkout_url,
            created=False,
            amount=existing.amount,
            currency=existing.currency,
            exchange_rate=_payment_exchange_rate(existing),
        )

    plan_id, promocion_id, clientes = _inter_assist_clientes(session, sale)
    if sale.departure_date is None or not sale.trip_type:
        raise SaleFlowError(400, "La venta no tiene datos de viaje")
    pending_url = data.return_url_pending.strip()
    try:
        checkout = create_inter_assist_checkout(
            gateway=gateway,
            plan_id=plan_id,
            trip_type=sale.trip_type,
            days_range=sale.days_range,
            departure_date=sale.departure_date,
            return_date=sale.return_date,
            clientes=clientes,
            promocion_id=promocion_id,
            success_url=data.return_url_ok.strip(),
            failure_url=data.return_url_error.strip(),
            pending_url=pending_url,
            notification_url=_inter_assist_notification_url(sale.id or 0),
            credit_card_id=card_id,
            installments=installments,
        )
    except InterAssistEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="process_payment",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {"gateway": gateway},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    if not checkout.checkout_url:
        raise SaleFlowError(502, "Inter Assist no devolvió el link de pago")
    if checkout.amount is None:
        raise SaleFlowError(502, "Inter Assist no devolvió el monto en pesos")
    currency = (checkout.currency or "ARS")[:3]
    if currency != "ARS":
        raise SaleFlowError(
            502,
            f"Inter Assist devolvió el cobro en {currency}; se esperaba ARS",
        )

    _reject_pending_payments(session, sale.id or 0)
    payment = Payment(
        sale_id=sale.id or 0,
        provider=gateway,
        status=PaymentStatus.PENDING.value,
        amount=checkout.amount,
        currency=currency,
        checkout_url=checkout.checkout_url[:2000],
        external_id=checkout.order_uuid[:80],
        financing_id=str(installments) if installments is not None else None,
        provider_data={
            "gateway": gateway,
            "order_uuid": checkout.order_uuid,
            "credit_card_id": card_id,
            "installments": installments,
            "exchange_rate": str(checkout.exchange_rate) if checkout.exchange_rate is not None else None,
            "quoted_amount": str(sale.amount),
            "quoted_currency": sale.currency,
            "return_url_ok": data.return_url_ok.strip(),
            "return_url_error": data.return_url_error.strip(),
            "return_url_pending": data.return_url_pending.strip(),
        },
    )
    session.add(payment)
    sale.payment_status = SalePaymentStatus.PENDING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="process_payment",
        success=True,
        http_status=checkout.status_code,
        request_payload=checkout.request_body,
        response_payload=checkout.response_body,
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(
        sale,
        checkout.checkout_url,
        created=True,
        amount=checkout.amount,
        currency=currency,
        exchange_rate=checkout.exchange_rate,
    )


def simulate_inter_assist_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentSimulate,
) -> PaymentResponse:
    """Mueve el pago simulado a pending, approved o rejected."""
    sale = _owned_inter_assist_sale(session, user, sale_id)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    payment = _pending_or_latest_simulated_payment(session, sale.id or 0)
    if payment is None:
        raise SaleFlowError(409, "No hay un pago simulado para esta venta")
    if payment.provider != PaymentProvider.SIMULATED.value:
        raise SaleFlowError(400, "Esta venta no usa la pasarela simulada")

    if data.result == "pending":
        payment.status = PaymentStatus.PENDING.value
        sale.payment_status = SalePaymentStatus.PENDING.value
    elif data.result == "approved":
        payment.status = PaymentStatus.APPROVED.value
        sale.payment_status = SalePaymentStatus.APPROVED.value
    else:
        payment.status = PaymentStatus.REJECTED.value
        sale.payment_status = SalePaymentStatus.REJECTED.value

    payment.updated_at = datetime.utcnow()
    sale.updated_at = datetime.utcnow()
    session.add(payment)
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="simulate_payment",
        success=True,
        http_status=200,
        request_payload={"result": data.result},
        response_payload={"payment_status": sale.payment_status},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, payment.checkout_url, created=False)


def confirm_inter_assist_payment(
    session: Session,
    user: User,
    sale_id: int,
) -> PaymentConfirmResponse:
    """Consulta la orden en Inter Assist y, si ya se emitió, guarda el voucher."""
    sale = _owned_inter_assist_sale(session, user, sale_id)
    return _confirm_inter_assist_sale(session, sale)


def open_universal_sale(
    session: Session,
    user: User,
    company_name: str,
    quote_id: str,
    quote: UniversalQuoteSnapshot,
) -> tuple[Sale, bool]:
    """Congela el lead de Universal. El voucher se pide después del pago simulado."""
    if _normalize_company(company_name) != _UNIVERSAL_SLUG:
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    if quote.origin.strip().upper() != "AR":
        raise SaleFlowError(400, "Universal solo se emite con origen Argentina")
    lead_id = quote_id.strip()
    if not lead_id or "/" in lead_id:
        raise SaleFlowError(400, "El lead de Universal no es válido")
    company = session.exec(select(Company).where(Company.slug == _UNIVERSAL_SLUG)).first()
    if company is None or company.id is None:
        raise SaleFlowError(400, "Universal no está cargada como compañía")
    if not company.active:
        raise SaleFlowError(400, "Universal no está activa")
    if quote.trip_type != "multiviaje" and quote.return_date is None:
        raise SaleFlowError(400, "Falta la fecha de regreso")
    try:
        currency, amount, destination = universal_charge(
            quote.destination_id,
            quote.amount_usd,
            quote.amount_local,
        )
    except UniversalEmissionError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc

    organization_id = resolve_universal_organization(session, user)
    if not organization_id:
        raise SaleFlowError(400, "Falta la organización de Universal")
    reference = f"universal:{lead_id}"
    existing = session.exec(select(Sale).where(Sale.provider_reference == reference)).first()
    if existing is not None:
        if existing.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta")
        if existing.emission_status in _LOCKED_STATUSES:
            return existing, False
        if existing.emission_status == EmissionStatus.PRODUCT_SELECTED.value:
            return existing, False

    product_id = (quote.product_id or lead_id).strip()
    context = {
        "lead_id": lead_id,
        "organization_id": organization_id,
        "destination": destination,
        "list_currency": currency,
        "amount_usd": str(quote.amount_usd),
        "amount_local": str(quote.amount_local) if quote.amount_local is not None else None,
        "product_name": (quote.product_name or quote.plan_name).strip(),
    }
    local_plan_id = _local_plan_id(session, company.id, product_id)
    if existing is None:
        seller_id, agency_id = _attribution(session, user)
        sale = Sale(
            created_by_user_id=user.id or 0,
            agency_id=agency_id,
            seller_id=seller_id,
            company_id=company.id,
            emission_status=EmissionStatus.PRODUCT_SELECTED.value,
            payment_status=SalePaymentStatus.NOT_STARTED.value,
            provider_reference=reference,
            origin="AR",
            destination_id=quote.destination_id,
            trip_type=quote.trip_type,
            days_range=quote.days_range,
            departure_date=quote.departure_date,
            return_date=quote.return_date,
            quoted_ages=list(quote.ages),
            external_plan_id=product_id[:80],
            plan_name=quote.plan_name[:200],
            plan_id=local_plan_id,
            currency=currency,
            amount=amount,
            markup=quote.markup,
            provider_context=context,
        )
        session.add(sale)
        created = True
    else:
        existing.emission_status = EmissionStatus.PRODUCT_SELECTED.value
        existing.destination_id = quote.destination_id
        existing.trip_type = quote.trip_type
        existing.days_range = quote.days_range
        existing.departure_date = quote.departure_date
        existing.return_date = quote.return_date
        existing.quoted_ages = list(quote.ages)
        existing.external_plan_id = product_id[:80]
        existing.plan_name = quote.plan_name[:200]
        existing.plan_id = local_plan_id
        existing.currency = currency
        existing.amount = amount
        existing.markup = quote.markup
        existing.provider_context = context
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        sale = existing
        created = False

    session.flush()
    _add_attempt(
        session,
        sale,
        success=True,
        http_status=200,
        request_payload={"lead_id": lead_id, "destination": destination, "currency": currency},
        response_payload={"provider_reference": reference},
        error_message=None,
        step="select_product",
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raced = session.exec(select(Sale).where(Sale.provider_reference == reference)).first()
        if raced is None or raced.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta") from exc
        return raced, False
    session.refresh(sale)
    return sale, created


def set_universal_passengers(
    session: Session,
    user: User,
    sale_id: int,
    data: PassengersCreate,
) -> PassengersResponse:
    """Guarda los pasajeros en local. Universal los recibe al emitir."""
    sale = _editable_universal_sale(session, user, sale_id)
    if sale.departure_date is None:
        raise SaleFlowError(400, "La venta no tiene fecha de salida")
    quoted_ages = [int(age) for age in (sale.quoted_ages or [])]
    if len(data.passengers) != len(quoted_ages):
        raise SaleFlowError(400, "La cantidad de pasajeros tiene que coincidir con la cotización")
    ages = [_age_on(passenger.birth_date, sale.departure_date) for passenger in data.passengers]
    if Counter(ages) != Counter(quoted_ages):
        raise SaleFlowError(400, "Las edades a la fecha de salida no coinciden con la cotización")

    _replace_passengers_generic(session, sale, data)
    sale.emergency_first_name = data.emergency_contact.first_name.strip()
    sale.emergency_last_name = data.emergency_contact.last_name.strip()
    sale.emergency_phone = data.emergency_contact.phone.strip()
    sale.emergency_email = (
        str(data.emergency_contact.email) if data.emergency_contact.email else None
    )
    sale.emission_status = EmissionStatus.PASSENGERS_COMPLETE.value
    sale.updated_at = datetime.utcnow()
    context = dict(sale.provider_context or {})
    context["payment_methods"] = [_SIMULATED_PAYMENT_METHOD]
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="assign_passengers",
        success=True,
        http_status=200,
        request_payload={"passengers": len(data.passengers)},
        response_payload={"payment_methods": [_SIMULATED_PAYMENT_METHOD]},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return passengers_response(session, sale)


def start_universal_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentStart,
) -> PaymentResponse:
    """Abre un cobro simulado. Universal no cobra."""
    sale = _payable_universal_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    existing = _pending_payment(session, sale.id or 0)
    if existing is not None and existing.checkout_url:
        return _payment_response(sale, existing.checkout_url, created=False)

    checkout_url = data.return_url_pending.strip() or data.return_url_ok.strip()
    payment = Payment(
        sale_id=sale.id or 0,
        provider=PaymentProvider.SIMULATED.value,
        status=PaymentStatus.PENDING.value,
        amount=sale.amount,
        currency=sale.currency,
        checkout_url=checkout_url[:2000],
        external_id="simulated",
        financing_id=None,
        provider_data={"simulated": True},
    )
    session.add(payment)
    sale.payment_status = SalePaymentStatus.PENDING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="process_payment",
        success=True,
        http_status=200,
        request_payload={"provider": PaymentProvider.SIMULATED.value},
        response_payload={"checkout_url": checkout_url},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, checkout_url, created=True)


def simulate_universal_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentSimulate,
) -> PaymentResponse:
    sale = _owned_universal_sale(session, user, sale_id)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    payment = _pending_or_latest_simulated_payment(session, sale.id or 0)
    if payment is None:
        raise SaleFlowError(409, "No hay un pago simulado para esta venta")
    if data.result == "pending":
        payment.status = PaymentStatus.PENDING.value
        sale.payment_status = SalePaymentStatus.PENDING.value
    elif data.result == "approved":
        payment.status = PaymentStatus.APPROVED.value
        sale.payment_status = SalePaymentStatus.APPROVED.value
    else:
        payment.status = PaymentStatus.REJECTED.value
        sale.payment_status = SalePaymentStatus.REJECTED.value
    payment.updated_at = datetime.utcnow()
    sale.updated_at = datetime.utcnow()
    session.add(payment)
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="simulate_payment",
        success=True,
        http_status=200,
        request_payload={"result": data.result},
        response_payload={"payment_status": sale.payment_status},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, payment.checkout_url, created=False)


def confirm_universal_payment(
    session: Session,
    user: User,
    sale_id: int,
) -> PaymentConfirmResponse:
    sale = _owned_universal_sale(session, user, sale_id)
    return _confirm_universal_sale(session, sale)


def _confirm_universal_sale(session: Session, sale: Sale) -> PaymentConfirmResponse:
    if sale.emission_status == EmissionStatus.ISSUED.value:
        return _confirm_response(
            sale, True, _voucher_for(session, sale.id or 0), company_slug=_UNIVERSAL_SLUG
        )
    if sale.payment_status == SalePaymentStatus.REJECTED.value:
        raise SaleFlowError(409, "El pago de esta venta fue rechazado")
    if sale.payment_status == SalePaymentStatus.PENDING.value:
        return _confirm_response(sale, False, None, company_slug=_UNIVERSAL_SLUG)
    if sale.payment_status != SalePaymentStatus.APPROVED.value:
        raise SaleFlowError(409, "El pago de esta venta no está iniciado")
    if _approved_simulated_payment(session, sale.id or 0) is None:
        raise SaleFlowError(409, "No hay un pago aprobado para esta venta")
    if sale.departure_date is None or not sale.trip_type:
        raise SaleFlowError(400, "La venta no tiene datos de viaje")

    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")
    context = dict(sale.provider_context or {})
    lead_id = str(context.get("lead_id") or "").strip()
    organization_id = str(context.get("organization_id") or "").strip()
    destination = str(context.get("destination") or "").strip()
    if not lead_id or not organization_id or not destination:
        raise SaleFlowError(400, "La venta no tiene el lead de Universal")

    sale.emission_status = EmissionStatus.EMITTING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    session.flush()
    try:
        result = emit_universal_voucher(
            lead_id=lead_id,
            control_number=f"DYS-UA-{sale.id}",
            organization_id=organization_id,
            plan_name=str(context.get("product_name") or sale.plan_name).strip(),
            destination=destination,
            currency=sale.currency,
            amount=sale.amount,
            departure_date=sale.departure_date,
            return_date=sale.return_date or sale.departure_date,
            trip_type=sale.trip_type,
            passengers=[
                UniversalPassengerDraft(
                    first_name=row.first_name,
                    last_name=row.last_name,
                    birth_date=row.birth_date,
                    document_type=row.document_type,
                    document_number=row.document_number,
                    email=row.email,
                )
                for row in passengers
            ],
        )
    except UniversalEmissionError as exc:
        sale.emission_status = EmissionStatus.EMISSION_FAILED.value
        sale.updated_at = datetime.utcnow()
        session.add(sale)
        _add_attempt(
            session,
            sale,
            step="emit_sale",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    try:
        retire_universal_lead(lead_id)
    except UniversalEmissionError:
        logger.warning("universal baja lead failed sale_id=%s lead=%s", sale.id, lead_id)

    sale.emission_status = EmissionStatus.ISSUED.value
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    voucher = Voucher(
        sale_id=sale.id or 0,
        passenger_id=None,
        external_id=result.number[:80],
        number=result.number[:80],
        url=_public_voucher_pdf_url(sale.id or 0)[:2000],
        status=VoucherStatus.ISSUED.value,
        provider_data={"organization_id": organization_id},
    )
    session.add(voucher)
    _add_attempt(
        session,
        sale,
        step="emit_sale",
        success=True,
        http_status=result.status_code,
        request_payload=result.request_body,
        response_payload={"voucher_number": result.number},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _confirm_response(sale, True, voucher, company_slug=_UNIVERSAL_SLUG)


def download_universal_voucher_pdf(
    session: Session,
    user: User,
    sale_id: int,
    lang: str = "es",
) -> tuple[bytes, str]:
    sale = _owned_universal_sale(
        session,
        user,
        sale_id,
        forbidden="No podés ver el voucher de esta venta",
        allow_admin=True,
    )
    if sale.emission_status != EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta todavía no tiene voucher emitido")
    voucher = _voucher_for(session, sale.id or 0)
    if voucher is None or not (voucher.number or "").strip():
        raise SaleFlowError(404, "No encontramos el voucher de esta venta")
    organization_id = str((voucher.provider_data or {}).get("organization_id") or "").strip()
    if not organization_id:
        organization_id = str((sale.provider_context or {}).get("organization_id") or "").strip()
    try:
        content = fetch_universal_voucher_pdf(voucher.number or "", organization_id, lang=lang)
    except UniversalEmissionError as exc:
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc
    return content, f"voucher-{voucher.number}.pdf"


def _editable_universal_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = _owned_universal_sale(
        session, user, sale_id, forbidden="No podés cargar pasajeros en esta venta"
    )
    if sale.emission_status not in (
        EmissionStatus.PRODUCT_SELECTED.value,
        EmissionStatus.PASSENGERS_COMPLETE.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cargar pasajeros")
    return sale


def _payable_universal_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = _owned_universal_sale(session, user, sale_id)
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.ISSUED.value,
        EmissionStatus.EMISSION_FAILED.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cobrar")
    return sale


def _owned_universal_sale(
    session: Session,
    user: User,
    sale_id: int,
    *,
    forbidden: str = "No podés cobrar esta venta",
    allow_admin: bool = False,
) -> Sale:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    owner = sale.created_by_user_id == user.id
    admin_reader = allow_admin and user.role == UserRole.ADMIN
    if not owner and not admin_reader:
        raise SaleFlowError(403, forbidden)
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _UNIVERSAL_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Universal")
    return sale


def confirm_inter_assist_from_notification(
    session: Session,
    sale_id: int,
) -> PaymentConfirmResponse:
    """Aviso de Inter Assist. El estado real lo dice el GET de la orden."""
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _INTER_ASSIST_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Inter Assist")
    return _confirm_inter_assist_sale(session, sale, from_notification=True)


def get_inter_assist_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    sale = _owned_inter_assist_sale(
        session, user, sale_id, forbidden="No podés ver esta venta"
    )
    return sale_detail(session, sale, _INTER_ASSIST_SLUG)


def list_sales(
    session: Session,
    user: User,
    *,
    emission_status: str | None = None,
    payment_status: str | None = None,
    company: str | None = None,
    agency_id: int | None = None,
    created_by_user_id: int | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SaleListResponse:
    """Ventas del usuario. El admin ve todas y puede pedir las de otra persona."""
    allowed_emission = {item.value for item in EmissionStatus}
    if emission_status is not None and emission_status not in allowed_emission:
        raise SaleFlowError(400, "emission_status no es válido")
    if payment_status is not None and payment_status not in {item.value for item in SalePaymentStatus}:
        raise SaleFlowError(400, "payment_status no es válido")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise SaleFlowError(400, "created_from no puede ser posterior a created_to")

    owner_id = created_by_user_id
    if user.role != UserRole.ADMIN:
        if created_by_user_id is not None and created_by_user_id != user.id:
            raise SaleFlowError(403, "No podés ver las ventas de otro usuario")
        owner_id = user.id

    conditions = []
    if owner_id is not None:
        conditions.append(Sale.created_by_user_id == owner_id)
    if emission_status is not None:
        conditions.append(Sale.emission_status == emission_status)
    if payment_status is not None:
        conditions.append(Sale.payment_status == payment_status)
    if agency_id is not None:
        conditions.append(Sale.agency_id == agency_id)
    if created_from is not None:
        conditions.append(Sale.created_at >= datetime.combine(created_from, time.min))
    if created_to is not None:
        conditions.append(
            Sale.created_at < datetime.combine(created_to + timedelta(days=1), time.min)
        )
    if company is not None:
        conditions.append(Company.slug == company.strip().lower().replace(" ", "_"))

    stmt = select(Sale, Company.slug).join(Company, Company.id == Sale.company_id)
    count_stmt = select(func.count()).select_from(Sale).join(Company, Company.id == Sale.company_id)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    stmt = stmt.order_by(Sale.created_at.desc(), Sale.id.desc()).offset(offset).limit(limit)

    count_result = session.exec(count_stmt).one()
    total = count_result[0] if isinstance(count_result, tuple) else count_result
    rows = session.exec(stmt).all()
    return SaleListResponse(
        items=[sale_to_response(sale, slug) for sale, slug in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


_CANCELLABLE_PAYMENTS = {
    SalePaymentStatus.NOT_STARTED.value,
    SalePaymentStatus.REJECTED.value,
}


def cancel_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    """Deja la venta en cancelled. La cotización sigue tomada."""
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id and user.role != UserRole.ADMIN:
        raise SaleFlowError(403, "No podés cancelar esta venta")
    if sale.emission_status != EmissionStatus.CANCELLED.value:
        if sale.payment_status not in _CANCELLABLE_PAYMENTS:
            raise SaleFlowError(
                409,
                "No se puede cancelar una venta con el pago pendiente o aprobado",
            )
        sale.emission_status = EmissionStatus.CANCELLED.value
        session.add(sale)
        _commit(session)
        session.refresh(sale)
    return _sale_detail_for_company(session, sale)


def _sale_detail_for_company(session: Session, sale: Sale) -> SaleDetailResponse:
    company = session.get(Company, sale.company_id)
    if company is None:
        raise SaleFlowError(400, "La compañía de la venta no existe")
    if company.slug == _OMINT_SLUG:
        return sale_detail(session, sale, _OMINT_SLUG)
    if company.slug == _INTER_ASSIST_SLUG:
        return sale_detail(session, sale, _INTER_ASSIST_SLUG)
    if company.slug == _UNIVERSAL_SLUG:
        return sale_detail(session, sale, _UNIVERSAL_SLUG)
    if company.slug == "terrawind":
        return sale_detail(session, sale, "terrawind")
    if company.slug == "new_travel":
        return sale_detail(session, sale, "new_travel")
    raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")


def get_sale_for_user(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id and user.role != UserRole.ADMIN:
        raise SaleFlowError(403, "No podés ver esta venta")
    return _sale_detail_for_company(session, sale)


def passengers_response(session: Session, sale: Sale) -> PassengersResponse:
    rows = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    return PassengersResponse(
        sale_id=sale.id or 0,
        emission_status=sale.emission_status,
        emergency_contact=_emergency_contact(sale),
        passengers=[_passenger_response(row) for row in rows],
        payment_methods=_payment_method_options(sale),
    )


def sale_detail(session: Session, sale: Sale, company_slug: str) -> SaleDetailResponse:
    if company_slug == _INTER_ASSIST_SLUG:
        _ensure_inter_assist_payment_methods(session, sale)
    rows = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    pending = _pending_payment(session, sale.id or 0)
    voucher = None
    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
    checkout_url = None
    payment_amount = None
    payment_currency = None
    exchange_rate = None
    payment_row = pending
    if payment_row is None:
        payment_row = _latest_payment(session, sale.id or 0)
    if pending is not None and sale.payment_status == SalePaymentStatus.PENDING.value:
        checkout_url = pending.checkout_url
    if company_slug == _INTER_ASSIST_SLUG and payment_row is not None:
        payment_amount = payment_row.amount
        payment_currency = payment_row.currency
        exchange_rate = _payment_exchange_rate(payment_row)
    base = sale_to_response(sale, company_slug)
    voucher_url = None
    vouchers: list[SaleVoucherItem] = []
    if company_slug == _INTER_ASSIST_SLUG and sale.emission_status == EmissionStatus.ISSUED.value:
        # Si hay más pax que vouchers guardados, rehidratar desde GET /api/ventas.
        if len(_inter_assist_voucher_ids(sale)) < len(rows):
            _refresh_inter_assist_vouchers_from_provider(session, sale)
            session.refresh(sale)
            if sale.emission_status == EmissionStatus.ISSUED.value:
                voucher = _voucher_for(session, sale.id or 0)
        vouchers = _inter_assist_voucher_items(sale)
        if vouchers:
            voucher_url = vouchers[0].url
        elif voucher is not None:
            voucher_url = _public_inter_assist_voucher_url(sale.id or 0)
    elif voucher is not None:
        voucher_url = (
            _public_voucher_pdf_url(sale.id or 0)
            if company_slug == _UNIVERSAL_SLUG
            else voucher.url
        )
    return SaleDetailResponse(
        **base.model_dump(),
        emergency_contact=_emergency_contact(sale),
        passengers=[_passenger_response(row) for row in rows],
        payment_methods=_payment_method_options(sale),
        checkout_url=checkout_url,
        voucher_number=(
            vouchers[0].number
            if vouchers
            else (voucher.number if voucher else None)
        ),
        voucher_url=voucher_url,
        vouchers=vouchers,
        payment_amount=payment_amount,
        payment_currency=payment_currency,
        exchange_rate=exchange_rate,
    )


def download_inter_assist_voucher_pdf(
    session: Session,
    user: User,
    sale_id: int,
    lang: str = "es",
    voucher_id: int | None = None,
) -> tuple[bytes, str]:
    """Proxy del PDF: el front no puede abrir la URL de Inter Assist (pide API Key)."""
    sale = _owned_inter_assist_sale(
        session,
        user,
        sale_id,
        forbidden="No podés ver el voucher de esta venta",
        allow_admin=True,
    )
    if sale.emission_status != EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta todavía no tiene voucher emitido")
    ids = _inter_assist_voucher_ids(sale)
    if not ids:
        raise SaleFlowError(404, "No encontramos el voucher de esta venta")
    if voucher_id is None:
        target_id = ids[0]
    else:
        if voucher_id not in ids:
            raise SaleFlowError(404, "Ese voucher no pertenece a esta venta")
        target_id = voucher_id
    content = _fetch_inter_assist_pdf(target_id, lang=lang)
    filename = f"voucher-{target_id}.pdf"
    return content, filename


def download_inter_assist_vouchers_zip(
    session: Session,
    user: User,
    sale_id: int,
    lang: str = "es",
) -> tuple[bytes, str]:
    """Zip con un PDF por voucher (útil con varios pasajeros)."""
    sale = _owned_inter_assist_sale(
        session,
        user,
        sale_id,
        forbidden="No podés ver el voucher de esta venta",
        allow_admin=True,
    )
    if sale.emission_status != EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta todavía no tiene voucher emitido")
    ids = _inter_assist_voucher_ids(sale)
    if not ids:
        raise SaleFlowError(404, "No encontramos vouchers de esta venta")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for vid in ids:
            content = _fetch_inter_assist_pdf(vid, lang=lang)
            archive.writestr(f"voucher-{vid}.pdf", content)
    filename = f"vouchers-sale-{sale.id}.zip"
    return buffer.getvalue(), filename


def _fetch_inter_assist_pdf(voucher_id: int, *, lang: str) -> bytes:
    try:
        return fetch_voucher_pdf(voucher_id, lang=lang)
    except InterAssistEmissionError as exc:
        status = exc.status_code if exc.status_code < 500 else 502
        if exc.status_code == 401:
            raise SaleFlowError(
                502,
                "Inter Assist rechazó el PDF (401). Revisá la API Key y el scope vouchers",
            ) from exc
        if exc.status_code == 403:
            raise SaleFlowError(
                502,
                "Inter Assist no autorizó el PDF (403). Falta el scope vouchers en la API Key",
            ) from exc
        raise SaleFlowError(status, exc.detail) from exc


def _public_voucher_pdf_url(sale_id: int) -> str:
    path = f"/v1/sales/{sale_id}/voucher/pdf"
    base = get_settings().public_api_base_url.strip().rstrip("/")
    return f"{base}{path}" if base else path


def _inter_assist_voucher_ids(sale: Sale) -> list[int]:
    context = sale.provider_context or {}
    raw_meta = context.get("vouchers")
    if isinstance(raw_meta, list) and raw_meta:
        ids: list[int] = []
        for item in raw_meta:
            if not isinstance(item, dict):
                continue
            try:
                value = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        if ids:
            return ids
    raw = context.get("voucher_ids") or []
    ids = []
    if isinstance(raw, list):
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
    return ids


def _inter_assist_voucher_items(sale: Sale) -> list[SaleVoucherItem]:
    sale_id = sale.id or 0
    context = sale.provider_context or {}
    meta_by_id: dict[int, dict] = {}
    raw_meta = context.get("vouchers")
    if isinstance(raw_meta, list):
        for item in raw_meta:
            if not isinstance(item, dict):
                continue
            try:
                vid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if vid > 0:
                meta_by_id[vid] = item

    items: list[SaleVoucherItem] = []
    for vid in _inter_assist_voucher_ids(sale):
        meta = meta_by_id.get(vid) or {}
        items.append(
            SaleVoucherItem(
                id=vid,
                number=str(vid),
                url=_public_inter_assist_voucher_url(sale_id, voucher_id=vid),
                first_name=(str(meta.get("first_name") or "").strip() or None),
                last_name=(str(meta.get("last_name") or "").strip() or None),
                document_number=(str(meta.get("document_number") or "").strip() or None),
            )
        )
    return items


def _public_inter_assist_voucher_url(sale_id: int, voucher_id: int | None = None) -> str:
    path = f"/v1/sales/{sale_id}/voucher/pdf"
    if voucher_id is not None:
        path = f"{path}?voucher_id={voucher_id}"
    base = get_settings().public_api_base_url.strip().rstrip("/")
    return f"{base}{path}" if base else path


def sale_to_response(sale: Sale, company_slug: str) -> SaleResponse:
    return SaleResponse(
        id=sale.id or 0,
        company=company_slug,
        emission_status=sale.emission_status,
        payment_status=sale.payment_status,
        created_by_user_id=sale.created_by_user_id,
        agency_id=sale.agency_id,
        seller_id=sale.seller_id,
        origin=sale.origin,
        destination_id=sale.destination_id,
        trip_type=sale.trip_type,
        days_range=sale.days_range,
        departure_date=sale.departure_date,
        return_date=sale.return_date,
        quoted_ages=list(sale.quoted_ages or []),
        external_plan_id=sale.external_plan_id,
        plan_name=sale.plan_name,
        plan_id=sale.plan_id,
        currency=sale.currency,
        amount=sale.amount,
        net_amount=sale.net_amount,
        base_amount=sale.base_amount,
        markup=sale.markup,
        created_at=sale.created_at,
    )


def _parse_quote_id(quote_id: str) -> tuple[str, str]:
    if "|" not in quote_id:
        raise SaleFlowError(
            400,
            "El id de Omint tiene que incluir la cotización y el producto",
        )
    quotation_id, product_id = quote_id.split("|", 1)
    quotation_id = quotation_id.strip()
    product_id = product_id.strip()
    if not quotation_id or not product_id or "/" in quotation_id or "/" in product_id:
        raise SaleFlowError(
            400,
            "El id de Omint tiene que incluir la cotización y el producto",
        )
    return quotation_id, product_id


def _normalize_company(company_name: str) -> str:
    return company_name.strip().lower().replace(" ", "_")


def _inter_assist_reference(user_id: int, plan_id: str, quote: InterAssistQuoteSnapshot) -> str:
    ages = ",".join(str(age) for age in quote.ages)
    raw = (
        f"{user_id}|{plan_id}|{quote.departure_date.isoformat()}|"
        f"{quote.return_date.isoformat() if quote.return_date else ''}|"
        f"{ages}|{quote.trip_type}|{quote.days_range or ''}|"
        f"{quote.destination_id}|{quote.promocion_id}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"inter_assist:{plan_id}:{digest}"


def _validate_inter_assist_quote_amount(plan_id: str, quote: InterAssistQuoteSnapshot) -> None:
    """Recalcula con el cotizador local; valida monto y promocion_id según doc Inter."""
    settings = get_settings()
    if not settings.interassist_api_key.strip():
        return
    try:
        request = QuoteRequest(
            departure_date=quote.departure_date,
            return_date=quote.return_date or quote.departure_date,
            ages=list(quote.ages),
            origin=quote.origin,
            destination_id=quote.destination_id,
            trip_type=quote.trip_type,
            days_range=quote.days_range,
        )
        plans = InterAssistQuoteProvider().get_quotes(request)
    except Exception:
        logger.exception("inter_assist quote recheck failed plan_id=%s", plan_id)
        return

    match = next((plan for plan in plans if str(plan.id) == str(plan_id)), None)
    if match is None:
        return

    sent_promo = int(quote.promocion_id or 0)
    plan_promo = int(match.promocion_id or 0)
    list_rate = match.base_rate if match.base_rate is not None else match.final_rate
    promo_rate = match.final_rate
    provider_amount = _inter_assist_provider_amount(quote)

    if sent_promo > 0 and plan_promo > 0 and sent_promo != plan_promo:
        raise SaleFlowError(
            400,
            f"promocion_id {sent_promo} no coincide con la promo cotizada del plan ({plan_promo})",
        )
    if sent_promo > 0 and plan_promo == 0:
        raise SaleFlowError(
            400,
            "Esa promoción no aplica en la cotización actual (solo descuento % plano)",
        )
    if (
        sent_promo == 0
        and plan_promo > 0
        and promo_rate is not None
        and promo_rate > 0
        and abs(provider_amount - promo_rate) / promo_rate <= _AMOUNT_TOLERANCE
    ):
        raise SaleFlowError(
            400,
            f"El precio incluye promoción; mandá promocion_id={plan_promo} (0 fuerza cobro sin promo)",
        )

    expected = promo_rate if sent_promo > 0 else list_rate
    if expected is None or expected <= 0:
        return
    expected_dec = Decimal(str(expected))
    delta = abs(expected_dec - provider_amount) / expected_dec
    if delta > _AMOUNT_TOLERANCE:
        raise SaleFlowError(
            400,
            "El precio enviado no coincide con la cotización de Inter Assist",
        )


def _inter_assist_provider_amount(quote: InterAssistQuoteSnapshot) -> Decimal:
    """Monto del proveedor sin markup de catálogo local."""
    if quote.net_amount is not None and quote.net_amount > 0:
        return Decimal(str(quote.net_amount))
    amount = Decimal(str(quote.amount))
    markup = Decimal(str(quote.markup or 0))
    if markup <= 0:
        return amount
    return (amount / (Decimal("1") + markup / Decimal("100"))).quantize(Decimal("0.01"))


def _confirm_inter_assist_sale(
    session: Session,
    sale: Sale,
    *,
    from_notification: bool = False,
) -> PaymentConfirmResponse:
    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
        return _confirm_response(
            sale, True, voucher, company_slug=_INTER_ASSIST_SLUG
        )

    payment = _latest_payment(session, sale.id or 0)
    if payment is not None and payment.provider in {
        PaymentProvider.MERCADOPAGO.value,
        PaymentProvider.PAYWAY.value,
    }:
        return _confirm_inter_assist_checkout(
            session, sale, payment, from_notification=from_notification
        )

    if sale.payment_status == SalePaymentStatus.REJECTED.value:
        if from_notification:
            return _confirm_response(sale, False, None, company_slug=_INTER_ASSIST_SLUG)
        raise SaleFlowError(409, "El pago de esta venta fue rechazado")
    if sale.payment_status == SalePaymentStatus.PENDING.value:
        return _confirm_response(
            sale, False, None, company_slug=_INTER_ASSIST_SLUG
        )
    if sale.payment_status != SalePaymentStatus.APPROVED.value:
        raise SaleFlowError(409, "El pago de esta venta no está iniciado")

    payment = _approved_simulated_payment(session, sale.id or 0)
    if payment is None:
        raise SaleFlowError(409, "No hay un pago aprobado para esta venta")

    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")

    plan_raw = (sale.provider_context or {}).get("plan_id") or sale.external_plan_id
    try:
        plan_id = int(str(plan_raw).strip())
    except ValueError as exc:
        raise SaleFlowError(400, "El plan de Inter Assist no es válido") from exc
    promocion_id = int((sale.provider_context or {}).get("promocion_id") or 0)
    if sale.departure_date is None or not sale.trip_type:
        raise SaleFlowError(400, "La venta no tiene datos de viaje")

    clientes = [
        InterAssistClientDraft(
            first_name=row.first_name,
            last_name=row.last_name,
            birth_date=row.birth_date,
            document_number=row.document_number,
            email=row.email,
            phone=row.phone,
        )
        for row in passengers
    ]

    sale.emission_status = EmissionStatus.EMITTING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    session.flush()

    try:
        result = emit_inter_assist_sale(
            plan_id=plan_id,
            trip_type=sale.trip_type,
            days_range=sale.days_range,
            departure_date=sale.departure_date,
            return_date=sale.return_date,
            clientes=clientes,
            promocion_id=promocion_id,
        )
    except InterAssistEmissionError as exc:
        sale.emission_status = EmissionStatus.EMISSION_FAILED.value
        sale.updated_at = datetime.utcnow()
        session.add(sale)
        _add_attempt(
            session,
            sale,
            step="emit_sale",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    context = dict(sale.provider_context or {})
    context["venta_id"] = result.venta_id
    context["voucher_ids"] = result.voucher_ids
    if result.warnings:
        context["warnings"] = result.warnings
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    sale.emission_status = EmissionStatus.ISSUED.value
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)

    voucher = _persist_inter_assist_issued_vouchers(
        session, sale, result.venta_id, result.voucher_ids
    )
    _add_attempt(
        session,
        sale,
        step="emit_sale",
        success=True,
        http_status=result.status_code,
        request_payload=result.request_body,
        response_payload=result.response_body,
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _confirm_response(
        sale, True, voucher, company_slug=_INTER_ASSIST_SLUG
    )


def _persist_inter_assist_issued_vouchers(
    session: Session,
    sale: Sale,
    venta_id: int | None,
    voucher_ids: list[int],
) -> Voucher | None:
    """Doc Inter: persistir cada voucher_id; detalle en GET /api/ventas/{id}."""
    infos = _load_inter_assist_voucher_infos(venta_id, voucher_ids)
    info_by_id = {info.id: info for info in infos if info.id > 0}
    # Union emit + GET: si /api/ventas trae menos pax, no pisar voucher_ids del emit.
    all_ids: list[int] = []
    for vid in list(voucher_ids) + [info.id for info in infos]:
        try:
            value = int(vid)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in all_ids:
            all_ids.append(value)
    resolved_infos = [
        info_by_id.get(vid) or InterAssistVoucherInfo(id=vid) for vid in all_ids
    ]
    context = dict(sale.provider_context or {})
    if venta_id:
        context["venta_id"] = venta_id
    context["voucher_ids"] = all_ids
    context["vouchers"] = [
        {
            "id": info.id,
            "first_name": info.first_name,
            "last_name": info.last_name,
            "document_number": info.document_number,
            "email": info.email,
        }
        for info in resolved_infos
    ]
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)

    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    by_dni = {
        (row.document_number or "").strip(): row
        for row in passengers
        if (row.document_number or "").strip()
    }
    first: Voucher | None = None
    for info in resolved_infos:
        passenger = by_dni.get((info.document_number or "").strip())
        row = _save_inter_assist_voucher(
            session,
            sale,
            info.id,
            all_ids,
            passenger_id=passenger.id if passenger is not None else None,
            info=info,
        )
        if first is None:
            first = row
    return first


def _load_inter_assist_voucher_infos(
    venta_id: int | None,
    voucher_ids: list[int],
) -> list[InterAssistVoucherInfo]:
    if venta_id:
        try:
            infos = get_inter_assist_venta_vouchers(venta_id)
            if infos:
                return infos
        except InterAssistEmissionError:
            logger.warning(
                "inter_assist venta vouchers unavailable venta_id=%s",
                venta_id,
                exc_info=True,
            )
    return [InterAssistVoucherInfo(id=vid) for vid in voucher_ids if vid > 0]


def _refresh_inter_assist_vouchers_from_provider(session: Session, sale: Sale) -> None:
    """Relee vouchers de Inter Assist cuando el context quedó incompleto (1 de N)."""
    context = sale.provider_context or {}
    venta_id = None
    try:
        raw = context.get("venta_id")
        if raw is not None:
            venta_id = int(raw)
    except (TypeError, ValueError):
        venta_id = None
    if not venta_id:
        return
    known = _inter_assist_voucher_ids(sale)
    try:
        _persist_inter_assist_issued_vouchers(session, sale, venta_id, known)
        _commit(session)
    except Exception:
        logger.warning(
            "inter_assist voucher refresh failed sale_id=%s venta_id=%s",
            sale.id,
            venta_id,
            exc_info=True,
        )


def _save_inter_assist_voucher(
    session: Session,
    sale: Sale,
    voucher_id: int,
    all_ids: list[int],
    *,
    passenger_id: int | None = None,
    info: InterAssistVoucherInfo | None = None,
) -> Voucher:
    external_id = str(voucher_id)
    existing = session.exec(
        select(Voucher).where(
            Voucher.sale_id == sale.id,
            Voucher.external_id == external_id,
        )
    ).first()
    provider_data = {
        "voucher_ids": all_ids,
        "venta_id": (sale.provider_context or {}).get("venta_id"),
    }
    if info is not None:
        provider_data.update(
            {
                "first_name": info.first_name,
                "last_name": info.last_name,
                "document_number": info.document_number,
                "email": info.email,
            }
        )
    if existing is not None:
        if passenger_id is not None and existing.passenger_id is None:
            existing.passenger_id = passenger_id
        existing.provider_data = {**(existing.provider_data or {}), **provider_data}
        session.add(existing)
        return existing
    voucher = Voucher(
        sale_id=sale.id or 0,
        passenger_id=passenger_id,
        external_id=external_id[:80],
        number=external_id[:80],
        url=_public_inter_assist_voucher_url(sale.id or 0, voucher_id=voucher_id)[:2000],
        status=VoucherStatus.ISSUED.value,
        provider_data=provider_data,
    )
    session.add(voucher)
    session.flush()
    return voucher


def _editable_inter_assist_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = _owned_inter_assist_sale(
        session, user, sale_id, forbidden="No podés cargar pasajeros en esta venta"
    )
    if sale.emission_status not in (
        EmissionStatus.PRODUCT_SELECTED.value,
        EmissionStatus.PASSENGERS_COMPLETE.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cargar pasajeros")
    return sale


def _payable_inter_assist_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = _owned_inter_assist_sale(session, user, sale_id)
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.ISSUED.value,
        EmissionStatus.EMISSION_FAILED.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cobrar")
    return sale


def _owned_inter_assist_sale(
    session: Session,
    user: User,
    sale_id: int,
    *,
    forbidden: str = "No podés cobrar esta venta",
    allow_admin: bool = False,
) -> Sale:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    owner = sale.created_by_user_id == user.id
    admin_reader = allow_admin and user.role == UserRole.ADMIN
    if not owner and not admin_reader:
        raise SaleFlowError(403, forbidden)
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _INTER_ASSIST_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Inter Assist")
    return sale


def _pending_or_latest_simulated_payment(session: Session, sale_id: int) -> Payment | None:
    pending = _pending_payment(session, sale_id)
    if pending is not None:
        return pending
    return session.exec(
        select(Payment)
        .where(
            Payment.sale_id == sale_id,
            Payment.provider == PaymentProvider.SIMULATED.value,
        )
        .order_by(Payment.id.desc())
    ).first()


def _approved_simulated_payment(session: Session, sale_id: int) -> Payment | None:
    return session.exec(
        select(Payment)
        .where(
            Payment.sale_id == sale_id,
            Payment.provider == PaymentProvider.SIMULATED.value,
            Payment.status == PaymentStatus.APPROVED.value,
        )
        .order_by(Payment.id.desc())
    ).first()


def _replace_passengers_generic(session: Session, sale: Sale, data: PassengersCreate) -> None:
    current = session.exec(select(Passenger).where(Passenger.sale_id == sale.id)).all()
    for row in current:
        session.delete(row)
    session.flush()
    for passenger in data.passengers:
        provider_data: dict = {}
        if passenger.country_id:
            provider_data["country_id"] = passenger.country_id.strip()
        if passenger.province_id:
            provider_data["province_id"] = passenger.province_id.strip()
        gender = passenger.gender.strip().lower() if passenger.gender else None
        session.add(
            Passenger(
                sale_id=sale.id or 0,
                is_holder=passenger.is_holder,
                first_name=passenger.first_name.strip(),
                last_name=passenger.last_name.strip(),
                birth_date=passenger.birth_date,
                document_type=passenger.document_type,
                document_number=passenger.document_number.strip(),
                gender=gender,
                email=str(passenger.email) if passenger.email else None,
                phone=passenger.phone.strip() if passenger.phone else None,
                country=passenger.country.strip() if passenger.country else None,
                province=passenger.province.strip() if passenger.province else None,
                city=passenger.city.strip() if passenger.city else None,
                street=passenger.street.strip() if passenger.street else None,
                street_number=passenger.street_number.strip() if passenger.street_number else None,
                provider_data=provider_data,
            )
        )
    session.flush()


def _upsert_sale(
    session: Session,
    *,
    user: User,
    company: Company,
    reference: str,
    existing: Sale | None,
    selection: OmintSelection,
    emission_status: str,
) -> Sale:
    plan_id = _local_plan_id(session, company.id or 0, selection.product_code)
    if existing is None:
        seller_id, agency_id = _attribution(session, user)
        sale = Sale(
            created_by_user_id=user.id or 0,
            agency_id=agency_id,
            seller_id=seller_id,
            company_id=company.id or 0,
            emission_status=emission_status,
            payment_status=SalePaymentStatus.NOT_STARTED.value,
            provider_reference=reference,
            quoted_ages=selection.ages,
            external_plan_id=selection.product_code[:80],
            plan_name=selection.plan_name[:200],
            plan_id=plan_id,
            currency=selection.currency[:3],
            amount=selection.amount,
            net_amount=None,
            base_amount=selection.base_amount,
            markup=Decimal("0"),
            provider_context=selection.provider_context,
        )
        _copy_trip(sale, selection)
        session.add(sale)
        session.flush()
        return sale

    existing.emission_status = emission_status
    existing.external_plan_id = selection.product_code[:80]
    existing.plan_name = selection.plan_name[:200]
    existing.plan_id = plan_id
    existing.currency = selection.currency[:3]
    existing.amount = selection.amount
    existing.base_amount = selection.base_amount
    existing.markup = Decimal("0")
    existing.quoted_ages = selection.ages
    existing.provider_context = selection.provider_context
    existing.updated_at = datetime.utcnow()
    _copy_trip(existing, selection)
    session.add(existing)
    session.flush()
    return existing


def _confirm_sale(session: Session, sale: Sale) -> PaymentConfirmResponse:
    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
        return _confirm_response(sale, True, voucher)
    if sale.payment_status != SalePaymentStatus.PENDING.value:
        raise SaleFlowError(409, "El pago de esta venta no está iniciado")
    quotation_id = str((sale.provider_context or {}).get("quotation_id") or "").strip()
    if not quotation_id:
        raise SaleFlowError(400, "La venta no tiene la cotización de Omint")
    try:
        confirmation = confirm_omint_checkout(quotation_id)
    except OmintEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="confirm_payment",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload={"quotationId": quotation_id},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        raise SaleFlowError(502, exc.detail) from exc

    if not confirmation.paid or not confirmation.voucher_id:
        _add_attempt(
            session,
            sale,
            step="confirm_payment",
            success=True,
            http_status=confirmation.http_status,
            request_payload={"quotationId": quotation_id},
            response_payload=confirmation.response_body,
            error_message="El pago todavía no está acreditado",
        )
        _commit(session)
        return _confirm_response(sale, False, None)

    voucher = _save_voucher(session, sale, confirmation)
    payment = _pending_payment(session, sale.id or 0)
    if payment is not None:
        payment.status = PaymentStatus.APPROVED.value
        payment.updated_at = datetime.utcnow()
        session.add(payment)
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.emission_status = EmissionStatus.ISSUED.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="confirm_payment",
        success=True,
        http_status=confirmation.http_status,
        request_payload={"quotationId": quotation_id},
        response_payload={
            "voucherId": confirmation.voucher_id,
            "voucherNumber": confirmation.voucher_number,
            "voucherExists": confirmation.voucher_exists,
        },
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _confirm_response(sale, True, voucher)


def _payable_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = _owned_omint_sale(session, user, sale_id)
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.ISSUED.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cobrar")
    return sale


def _owned_omint_sale(
    session: Session,
    user: User,
    sale_id: int,
    *,
    forbidden: str = "No podés cobrar esta venta",
) -> Sale:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id:
        raise SaleFlowError(403, forbidden)
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _OMINT_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Omint")
    return sale


def _check_return_urls(data: PaymentStart) -> None:
    for label, url in (
        ("ok", data.return_url_ok),
        ("error", data.return_url_error),
        ("pendiente", data.return_url_pending),
    ):
        if not url.strip().lower().startswith(("https://", "http://")):
            raise SaleFlowError(400, f"La URL de retorno {label} no es válida")


def _notification_url(sale_id: int) -> str:
    base = get_settings().public_api_base_url.strip().rstrip("/")
    if not base.lower().startswith(("https://", "http://")):
        raise SaleFlowError(
            400,
            "Falta PUBLIC_API_BASE_URL para recibir la notificación de pago",
        )
    return f"{base}/v1/sales/{sale_id}/omint/payment-notification"


def _emergency_contact(sale: Sale) -> EmergencyContactResponse:
    return EmergencyContactResponse(
        first_name=sale.emergency_first_name,
        last_name=sale.emergency_last_name,
        phone=sale.emergency_phone,
        email=sale.emergency_email,
    )


def _load_payway_catalog() -> PaywayCardsResult | None:
    try:
        return list_payway_cards()
    except InterAssistEmissionError:
        logger.warning("inter_assist payway cards unavailable", exc_info=True)
        return None


def _inter_assist_payment_methods(catalog: PaywayCardsResult | None) -> list[dict]:
    methods: list[dict] = [
        {"id": "mercadopago", "code": "MERCADOPAGO", "financing": []},
    ]
    if catalog is None:
        return methods
    methods.append(
        {
            "id": "payway",
            "code": "PAYWAY",
            "financing": [
                {"id": str(dues), "dues": dues}
                for dues in range(1, catalog.max_installments + 1)
            ],
            "cards": [
                {"id": str(card.id), "name": card.name} for card in catalog.cards
            ],
            "max_installments": catalog.max_installments,
        }
    )
    return methods


def _ensure_inter_assist_payment_methods(session: Session, sale: Sale) -> None:
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.EMISSION_FAILED.value,
    ):
        return
    raw = (sale.provider_context or {}).get("payment_methods") or []
    if not isinstance(raw, list):
        raw = []
    ids = {str(item.get("id") or "") for item in raw if isinstance(item, dict)}
    if "mercadopago" in ids and "payway" in ids:
        return
    catalog = _load_payway_catalog()
    methods = _inter_assist_payment_methods(catalog)
    if methods == raw:
        return
    context = dict(sale.provider_context or {})
    context["payment_methods"] = methods
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)
    _commit(session)
    session.refresh(sale)


def _local_return_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _inter_assist_gateway(payment_method_id: str) -> str:
    method_id = (payment_method_id or "").strip().lower()
    if method_id in {"mercadopago", "mp"}:
        return PaymentProvider.MERCADOPAGO.value
    if method_id == "payway":
        return PaymentProvider.PAYWAY.value
    raise SaleFlowError(400, "Elegí mercadopago o payway")


def _payway_selection(
    sale: Sale,
    data: PaymentStart,
    gateway: str,
) -> tuple[int | None, int | None]:
    if gateway != PaymentProvider.PAYWAY.value:
        return None, None
    card_id = _dues(data.credit_card_id)
    installments = _dues(data.financing_id)
    if card_id is None:
        raise SaleFlowError(400, "Payway requiere credit_card_id")
    if installments is None:
        raise SaleFlowError(400, "Payway requiere financing_id con la cantidad de cuotas")
    payway = _stored_payway_method(sale)
    cards = payway.get("cards") if isinstance(payway, dict) else None
    known = set()
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict):
                parsed = _dues(card.get("id"))
                if parsed is not None:
                    known.add(parsed)
    if known and card_id not in known:
        raise SaleFlowError(400, "Esa tarjeta no está habilitada en Payway")
    max_installments = _dues(payway.get("max_installments")) if isinstance(payway, dict) else None
    if max_installments is None:
        max_installments = 6
    if installments < 1 or installments > max_installments:
        raise SaleFlowError(
            400,
            f"Las cuotas de Payway tienen que estar entre 1 y {max_installments}",
        )
    return card_id, installments


def _stored_payway_method(sale: Sale) -> dict:
    methods = (sale.provider_context or {}).get("payment_methods") or []
    if isinstance(methods, list):
        for method in methods:
            if isinstance(method, dict) and str(method.get("id") or "") == "payway":
                return method
    return {}


def _reusable_inter_assist_checkout(
    payment: Payment,
    gateway: str,
    card_id: int | None,
    installments: int | None,
    data: PaymentStart,
) -> bool:
    if payment.status != PaymentStatus.PENDING.value or not payment.checkout_url:
        return False
    if payment.provider != gateway:
        return False
    stored = payment.provider_data or {}
    if stored.get("return_url_ok") != data.return_url_ok.strip():
        return False
    if stored.get("return_url_error") != data.return_url_error.strip():
        return False
    if gateway == PaymentProvider.MERCADOPAGO.value:
        return stored.get("return_url_pending") == data.return_url_pending.strip()
    return stored.get("credit_card_id") == card_id and stored.get("installments") == installments


def _reject_pending_payments(session: Session, sale_id: int) -> None:
    rows = session.exec(
        select(Payment).where(
            Payment.sale_id == sale_id,
            Payment.status == PaymentStatus.PENDING.value,
        )
    ).all()
    now = datetime.utcnow()
    for row in rows:
        row.status = PaymentStatus.REJECTED.value
        row.updated_at = now
        session.add(row)


def _inter_assist_clientes(
    session: Session,
    sale: Sale,
) -> tuple[int, int, list[InterAssistClientDraft]]:
    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")
    if not any(_real_passenger_email(row.email) for row in passengers):
        raise SaleFlowError(
            400,
            "Al menos un viajero tiene que tener un email real para abrir el checkout",
        )
    plan_raw = (sale.provider_context or {}).get("plan_id") or sale.external_plan_id
    try:
        plan_id = int(str(plan_raw).strip())
    except ValueError as exc:
        raise SaleFlowError(400, "El plan de Inter Assist no es válido") from exc
    # Doc Inter §5.4 / §7.5: 0 = sin promo. El id sale del snapshot al abrir la venta.
    promocion_id = int((sale.provider_context or {}).get("promocion_id") or 0)
    clientes = [
        InterAssistClientDraft(
            first_name=row.first_name,
            last_name=row.last_name,
            birth_date=row.birth_date,
            document_number=row.document_number,
            email=row.email,
            phone=row.phone,
        )
        for row in passengers
    ]
    return plan_id, promocion_id, clientes


def _real_passenger_email(email: str | None) -> bool:
    value = (email or "").strip().lower()
    return bool(value) and "@" in value and value != "correo@correo.com"


def _inter_assist_notification_url(sale_id: int) -> str | None:
    base = get_settings().public_api_base_url.strip().rstrip("/")
    if not base.lower().startswith(("https://", "http://")):
        return None
    return f"{base}/v1/sales/{sale_id}/interassist/payment-notification"


def _latest_payment(session: Session, sale_id: int) -> Payment | None:
    return session.exec(
        select(Payment).where(Payment.sale_id == sale_id).order_by(Payment.id.desc())
    ).first()


def _confirm_inter_assist_checkout(
    session: Session,
    sale: Sale,
    payment: Payment,
    *,
    from_notification: bool,
) -> PaymentConfirmResponse:
    order_uuid = (payment.external_id or "").strip()
    if not order_uuid:
        raise SaleFlowError(400, "Esta venta no tiene orden de checkout")
    try:
        order = get_inter_assist_checkout(order_uuid)
    except InterAssistEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="confirm_payment",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload=exc.request_body or {"order_uuid": order_uuid},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        status = exc.status_code if exc.status_code < 500 else 502
        raise SaleFlowError(status, exc.detail) from exc

    _add_attempt(
        session,
        sale,
        step="confirm_payment",
        success=True,
        http_status=order.status_code,
        request_payload={"order_uuid": order_uuid},
        response_payload=order.response_body,
        error_message=None,
    )
    if order.status == "approved" and order.venta_id and order.voucher_ids:
        return _issue_inter_assist_checkout(session, sale, payment, order)

    if order.status in {"rejected", "cancelled"}:
        payment.status = PaymentStatus.REJECTED.value
        payment.updated_at = datetime.utcnow()
        sale.payment_status = SalePaymentStatus.REJECTED.value
        sale.updated_at = datetime.utcnow()
        session.add(payment)
        session.add(sale)
        _commit(session)
        session.refresh(sale)
        if from_notification:
            return _confirm_response(sale, False, None, company_slug=_INTER_ASSIST_SLUG)
        raise SaleFlowError(409, "El pago de esta venta fue rechazado")

    payment.status = PaymentStatus.PENDING.value
    payment.updated_at = datetime.utcnow()
    sale.payment_status = SalePaymentStatus.PENDING.value
    sale.updated_at = datetime.utcnow()
    session.add(payment)
    session.add(sale)
    _commit(session)
    session.refresh(sale)
    return _confirm_response(sale, False, None, company_slug=_INTER_ASSIST_SLUG)


def _issue_inter_assist_checkout(
    session: Session,
    sale: Sale,
    payment: Payment,
    order: InterAssistCheckoutResult,
) -> PaymentConfirmResponse:
    context = dict(sale.provider_context or {})
    context["venta_id"] = order.venta_id
    context["voucher_ids"] = order.voucher_ids
    context["checkout_gateway"] = order.gateway or payment.provider
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    sale.emission_status = EmissionStatus.ISSUED.value
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.updated_at = datetime.utcnow()
    payment.status = PaymentStatus.APPROVED.value
    payment.updated_at = datetime.utcnow()
    if order.amount is not None:
        payment.amount = order.amount
    if order.currency:
        payment.currency = order.currency[:3]
    if order.exchange_rate is not None:
        provider_data = dict(payment.provider_data or {})
        provider_data["exchange_rate"] = str(order.exchange_rate)
        payment.provider_data = provider_data
    session.add(payment)
    session.add(sale)
    voucher = _persist_inter_assist_issued_vouchers(
        session, sale, order.venta_id, order.voucher_ids
    )
    _commit(session)
    session.refresh(sale)
    return _confirm_response(sale, True, voucher, company_slug=_INTER_ASSIST_SLUG)


def _payment_method_options(sale: Sale) -> list[PaymentMethodOption]:
    raw = (sale.provider_context or {}).get("payment_methods") or []
    if not isinstance(raw, list):
        return []
    options: list[PaymentMethodOption] = []
    for method in raw:
        if not isinstance(method, dict):
            continue
        method_id = str(method.get("id") or "").strip()
        if not method_id:
            continue
        code = str(method.get("code") or "").strip().upper()
        options.append(
            PaymentMethodOption(
                id=method_id[:80],
                code=code[:40],
                name=PAYMENT_METHOD_NAMES.get(code, code)[:80],
                requires_financing=code in FINANCING_REQUIRED_CODES or code == "PAYWAY",
                financing=_financing_options(method.get("financing")),
                cards=_card_options(method.get("cards")),
                max_installments=_dues(method.get("max_installments")),
            )
        )
    return options


def _card_options(raw: object) -> list[PaywayCardOption]:
    if not isinstance(raw, list):
        return []
    cards: list[PaywayCardOption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        card_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or item.get("nombre") or "").strip()
        if not card_id or not name:
            continue
        cards.append(PaywayCardOption(id=card_id[:80], name=name[:80]))
    return cards


def _financing_options(raw: object) -> list[FinancingOption]:
    if not isinstance(raw, list):
        return []
    options: list[FinancingOption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        financing_id = str(item.get("id") or "").strip()
        if not financing_id:
            continue
        options.append(FinancingOption(id=financing_id[:80], dues=_dues(item.get("dues"))))
    return options


def _dues(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _payment_method(sale: Sale, payment_method_id: str) -> dict:
    methods = (sale.provider_context or {}).get("payment_methods") or []
    if not isinstance(methods, list) or not methods:
        raise SaleFlowError(400, "Omint no informó medios de pago para esta venta")
    for method in methods:
        if isinstance(method, dict) and _same_id(method.get("id"), payment_method_id):
            return method
    raise SaleFlowError(400, "Ese medio de pago no está disponible para esta venta")


def _financing_belongs(method: dict, financing_id: str) -> bool:
    options = method.get("financing") or []
    if not isinstance(options, list):
        return False
    return any(isinstance(option, dict) and _same_id(option.get("id"), financing_id) for option in options)


def _pending_payment(session: Session, sale_id: int) -> Payment | None:
    return session.exec(
        select(Payment)
        .where(Payment.sale_id == sale_id, Payment.status == PaymentStatus.PENDING.value)
        .order_by(Payment.id.desc())
    ).first()


def _payment_exchange_rate(payment: Payment) -> Decimal | None:
    raw = (payment.provider_data or {}).get("exchange_rate")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _payment_response(
    sale: Sale,
    checkout_url: str | None,
    *,
    created: bool,
    amount: Decimal | None = None,
    currency: str | None = None,
    exchange_rate: Decimal | None = None,
) -> PaymentResponse:
    return PaymentResponse(
        sale_id=sale.id or 0,
        emission_status=sale.emission_status,
        payment_status=sale.payment_status,
        checkout_url=checkout_url,
        created=created,
        amount=amount,
        currency=currency,
        exchange_rate=exchange_rate,
    )


def _save_voucher(session: Session, sale: Sale, confirmation) -> Voucher:
    existing = session.exec(
        select(Voucher).where(
            Voucher.sale_id == sale.id,
            Voucher.external_id == (confirmation.voucher_id or ""),
        )
    ).first()
    if existing is not None:
        return existing
    voucher = Voucher(
        sale_id=sale.id or 0,
        passenger_id=None,
        external_id=(confirmation.voucher_id or "")[:80],
        number=(confirmation.voucher_number or None) and confirmation.voucher_number[:80],
        url=(confirmation.voucher_url or None) and confirmation.voucher_url[:2000],
        status=VoucherStatus.ISSUED.value,
        provider_data={"voucher_exists": confirmation.voucher_exists},
    )
    session.add(voucher)
    session.flush()
    return voucher


def _voucher_for(session: Session, sale_id: int) -> Voucher | None:
    return session.exec(select(Voucher).where(Voucher.sale_id == sale_id).order_by(Voucher.id.desc())).first()


def _confirm_response(
    sale: Sale,
    paid: bool,
    voucher: Voucher | None,
    *,
    company_slug: str | None = None,
) -> PaymentConfirmResponse:
    voucher_url = None
    vouchers: list[SaleVoucherItem] = []
    if company_slug == _INTER_ASSIST_SLUG:
        vouchers = _inter_assist_voucher_items(sale)
        if vouchers:
            voucher_url = vouchers[0].url
        elif voucher is not None:
            voucher_url = _public_inter_assist_voucher_url(sale.id or 0)
    elif voucher is not None:
        if company_slug == _UNIVERSAL_SLUG:
            voucher_url = _public_voucher_pdf_url(sale.id or 0)
        else:
            voucher_url = voucher.url
    return PaymentConfirmResponse(
        sale_id=sale.id or 0,
        emission_status=sale.emission_status,
        payment_status=sale.payment_status,
        payment_received=paid,
        voucher_number=(
            vouchers[0].number
            if vouchers
            else (voucher.number if voucher else None)
        ),
        voucher_url=voucher_url,
        vouchers=vouchers,
    )


def _copy_trip(sale: Sale, selection: OmintSelection) -> None:
    sale.origin = selection.origin
    sale.destination_id = selection.destination_id
    sale.trip_type = selection.trip_type
    sale.days_range = selection.days_range
    sale.departure_date = selection.departure_date
    sale.return_date = selection.return_date


def _editable_omint_sale(session: Session, user: User, sale_id: int) -> Sale:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id:
        raise SaleFlowError(403, "No podés cargar pasajeros en esta venta")
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _OMINT_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Omint")
    if sale.emission_status not in (
        EmissionStatus.PRODUCT_SELECTED.value,
        EmissionStatus.PASSENGERS_COMPLETE.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cargar pasajeros")
    return sale


def _draft(passenger: PassengerInput) -> OmintPassengerDraft:
    return OmintPassengerDraft(
        first_name=passenger.first_name.strip(),
        last_name=passenger.last_name.strip(),
        birth_date=passenger.birth_date,
        document_type=passenger.document_type,
        document_number=passenger.document_number.strip(),
        is_holder=passenger.is_holder,
        email=str(passenger.email) if passenger.email else None,
        phone=passenger.phone.strip() if passenger.phone else None,
        country_id=passenger.country_id.strip() if passenger.country_id else None,
        province_id=passenger.province_id.strip() if passenger.province_id else None,
        street=passenger.street.strip() if passenger.street else None,
        street_number=passenger.street_number.strip() if passenger.street_number else None,
        city=passenger.city.strip() if passenger.city else None,
    )


def _replace_passengers(session: Session, sale: Sale, data: PassengersCreate) -> None:
    current = session.exec(select(Passenger).where(Passenger.sale_id == sale.id)).all()
    for row in current:
        session.delete(row)
    session.flush()
    for passenger in data.passengers:
        provider_data: dict = {
            "document_type_id": DOCUMENT_TYPE_IDS[passenger.document_type],
        }
        if passenger.country_id:
            provider_data["country_id"] = passenger.country_id.strip()
        if passenger.province_id:
            provider_data["province_id"] = passenger.province_id.strip()
        session.add(
            Passenger(
                sale_id=sale.id or 0,
                is_holder=passenger.is_holder,
                first_name=passenger.first_name.strip(),
                last_name=passenger.last_name.strip(),
                birth_date=passenger.birth_date,
                document_type=passenger.document_type,
                document_number=passenger.document_number.strip(),
                email=str(passenger.email) if passenger.email else None,
                phone=passenger.phone.strip() if passenger.phone else None,
                country=passenger.country.strip() if passenger.country else None,
                province=passenger.province.strip() if passenger.province else None,
                city=passenger.city.strip() if passenger.city else None,
                street=passenger.street.strip() if passenger.street else None,
                street_number=passenger.street_number.strip() if passenger.street_number else None,
                provider_data=provider_data,
            )
        )
    session.flush()


def _passenger_response(row: Passenger) -> PassengerResponse:
    data = row.provider_data or {}
    return PassengerResponse(
        id=row.id or 0,
        is_holder=row.is_holder,
        first_name=row.first_name,
        last_name=row.last_name,
        birth_date=row.birth_date,
        document_type=row.document_type,
        document_number=row.document_number,
        gender=row.gender,
        email=row.email,
        phone=row.phone,
        country=row.country,
        province=row.province,
        city=row.city,
        street=row.street,
        street_number=row.street_number,
        country_id=data.get("country_id"),
        province_id=data.get("province_id"),
    )


def _age_on(birth_date: date, on: date) -> int:
    years = on.year - birth_date.year
    if (on.month, on.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _add_attempt(
    session: Session,
    sale: Sale,
    *,
    success: bool,
    http_status: int | None,
    request_payload: dict,
    response_payload: dict,
    error_message: str | None,
    step: str = "assign_product",
) -> None:
    session.add(
        EmissionAttempt(
            sale_id=sale.id or 0,
            step=step,
            http_status=http_status,
            success=success,
            error_message=error_message[:500] if error_message else None,
            request_payload=request_payload,
            response_payload=response_payload or {},
        )
    )


def _attribution(session: Session, user: User) -> tuple[int | None, int | None]:
    if user.role == UserRole.SELLER:
        seller = session.exec(select(Seller).where(Seller.user_id == user.id)).first()
        if seller is None:
            return None, None
        return seller.id, seller.agency_id
    if user.role == UserRole.AGENCY:
        return None, user.agency_id
    return None, None


def _local_plan_id(session: Session, company_id: int, product_code: str) -> int | None:
    plan = session.exec(
        select(Plan).where(
            Plan.company_id == company_id,
            Plan.external_plan_id == product_code,
            Plan.active == True,  # noqa: E712
        )
    ).first()
    return plan.id if plan is not None else None


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise SaleFlowError(409, "Esa cotización ya tiene una venta") from exc


def _same_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left).strip().lower() == str(right).strip().lower()
