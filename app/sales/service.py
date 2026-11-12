"""Abre una venta de Omint al elegir un plan y asigna el producto en su API."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import get_settings
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
    PaymentSimulate,
    PaymentStart,
    SaleDetailResponse,
    SaleResponse,
)
from app.services.companies.inter_assist import InterAssistQuoteProvider
from app.services.companies.inter_assist_catalog import (
    list_inter_assist_countries,
    list_inter_assist_provinces,
)
from app.services.companies.inter_assist_emission import (
    InterAssistClientDraft,
    InterAssistEmissionError,
    emit_inter_assist_sale,
    emission_type_and_periodo,
    fetch_voucher_pdf,
    voucher_pdf_url,
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
        "promocion_id": quote.promocion_id,
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
    context = dict(sale.provider_context or {})
    context["receive_promotions"] = data.receive_promotions
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
    """Abre un cobro simulado (pasarela propia). No llama a Inter Assist."""
    sale = _payable_inter_assist_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    existing = _pending_payment(session, sale.id or 0)
    if existing is not None and existing.checkout_url:
        return _payment_response(sale, existing.checkout_url, created=False)

    checkout_url = (
        data.return_url_pending.strip()
        or data.return_url_ok.strip()
    )
    payment = Payment(
        sale_id=sale.id or 0,
        provider=PaymentProvider.SIMULATED.value,
        status=PaymentStatus.PENDING.value,
        amount=sale.amount,
        currency=sale.currency,
        checkout_url=checkout_url[:2000],
        external_id=(data.payment_method_id or "simulated").strip()[:80],
        financing_id=None,
        provider_data={
            "simulated": True,
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
        http_status=200,
        request_payload={"provider": PaymentProvider.SIMULATED.value},
        response_payload={"checkout_url": checkout_url},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, checkout_url, created=True)


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
    """Si el pago simulado está aprobado, emite en Inter Assist y guarda vouchers."""
    sale = _owned_inter_assist_sale(session, user, sale_id)
    return _confirm_inter_assist_sale(session, sale)


def get_inter_assist_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    sale = _owned_inter_assist_sale(
        session, user, sale_id, forbidden="No podés ver esta venta"
    )
    return sale_detail(session, sale, _INTER_ASSIST_SLUG)


def get_sale_for_user(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id:
        raise SaleFlowError(403, "No podés ver esta venta")
    company = session.get(Company, sale.company_id)
    if company is None:
        raise SaleFlowError(400, "La compañía de la venta no existe")
    if company.slug == _OMINT_SLUG:
        return sale_detail(session, sale, _OMINT_SLUG)
    if company.slug == _INTER_ASSIST_SLUG:
        return sale_detail(session, sale, _INTER_ASSIST_SLUG)
    raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")


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
    rows = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    pending = _pending_payment(session, sale.id or 0)
    voucher = None
    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
    checkout_url = None
    if pending is not None and sale.payment_status == SalePaymentStatus.PENDING.value:
        checkout_url = pending.checkout_url
    base = sale_to_response(sale, company_slug)
    voucher_url = None
    if voucher is not None:
        voucher_url = (
            _public_inter_assist_voucher_url(sale.id or 0)
            if company_slug == _INTER_ASSIST_SLUG
            else voucher.url
        )
    return SaleDetailResponse(
        **base.model_dump(),
        emergency_contact=_emergency_contact(sale),
        passengers=[_passenger_response(row) for row in rows],
        payment_methods=_payment_method_options(sale),
        checkout_url=checkout_url,
        voucher_number=voucher.number if voucher else None,
        voucher_url=voucher_url,
    )


def download_inter_assist_voucher_pdf(
    session: Session,
    user: User,
    sale_id: int,
    lang: str = "es",
) -> tuple[bytes, str]:
    """Proxy del PDF: el front no puede abrir la URL de Inter Assist (pide API Key)."""
    sale = _owned_inter_assist_sale(
        session, user, sale_id, forbidden="No podés ver el voucher de esta venta"
    )
    if sale.emission_status != EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta todavía no tiene voucher emitido")
    voucher = _voucher_for(session, sale.id or 0)
    if voucher is None or not (voucher.external_id or "").strip():
        raise SaleFlowError(404, "No encontramos el voucher de esta venta")
    try:
        voucher_id = int(str(voucher.external_id).strip())
    except ValueError as exc:
        raise SaleFlowError(502, "El id del voucher de Inter Assist no es válido") from exc
    try:
        content = fetch_voucher_pdf(voucher_id, lang=lang)
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
    filename = f"voucher-{voucher.number or voucher_id}.pdf"
    return content, filename


def _public_inter_assist_voucher_url(sale_id: int) -> str:
    path = f"/v1/sales/{sale_id}/voucher/pdf"
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
    """Recalcula con el cotizador local; si hay match y diverge mucho, rechaza."""
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
    quoted = Decimal(str(match.final_rate))
    if quoted <= 0:
        return
    delta = abs(quoted - quote.amount) / quoted
    if delta > _AMOUNT_TOLERANCE:
        raise SaleFlowError(
            400,
            "El precio enviado no coincide con la cotización de Inter Assist",
        )


def _confirm_inter_assist_sale(session: Session, sale: Sale) -> PaymentConfirmResponse:
    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
        return _confirm_response(
            sale, True, voucher, company_slug=_INTER_ASSIST_SLUG
        )

    if sale.payment_status == SalePaymentStatus.REJECTED.value:
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

    primary_voucher_id = result.voucher_ids[0]
    voucher = _save_inter_assist_voucher(session, sale, primary_voucher_id, result.voucher_ids)
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


def _save_inter_assist_voucher(
    session: Session,
    sale: Sale,
    voucher_id: int,
    all_ids: list[int],
) -> Voucher:
    external_id = str(voucher_id)
    existing = session.exec(
        select(Voucher).where(
            Voucher.sale_id == sale.id,
            Voucher.external_id == external_id,
        )
    ).first()
    if existing is not None:
        return existing
    voucher = Voucher(
        sale_id=sale.id or 0,
        passenger_id=None,
        external_id=external_id[:80],
        number=external_id[:80],
        url=voucher_pdf_url(voucher_id)[:2000],
        status=VoucherStatus.ISSUED.value,
        provider_data={"voucher_ids": all_ids, "venta_id": (sale.provider_context or {}).get("venta_id")},
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
) -> Sale:
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id:
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
                requires_financing=code in FINANCING_REQUIRED_CODES,
                financing=_financing_options(method.get("financing")),
            )
        )
    return options


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


def _payment_response(sale: Sale, checkout_url: str | None, *, created: bool) -> PaymentResponse:
    return PaymentResponse(
        sale_id=sale.id or 0,
        emission_status=sale.emission_status,
        payment_status=sale.payment_status,
        checkout_url=checkout_url,
        created=created,
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
    if voucher is not None:
        if company_slug == _INTER_ASSIST_SLUG:
            voucher_url = _public_inter_assist_voucher_url(sale.id or 0)
        else:
            voucher_url = voucher.url
    return PaymentConfirmResponse(
        sale_id=sale.id or 0,
        emission_status=sale.emission_status,
        payment_status=sale.payment_status,
        payment_received=paid,
        voucher_number=voucher.number if voucher else None,
        voucher_url=voucher_url,
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
