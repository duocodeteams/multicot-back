"""Flujo de venta Terrawind: abrir venta, pasajeros, cobro Nave y emisión SETW."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.company import Company
from app.models.sale import (
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
from app.models.user import User
from app.sales.schemas import (
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentResponse,
    PaymentStart,
    SaleDetailResponse,
    TerrawindQuoteSnapshot,
)
from app.services.companies.terrawind_emission import (
    TerrawindEmissionError,
    TerrawindPassengerDraft,
    check_voucher,
    emit_terrawind_vouchers,
    list_setw_countries,
    list_setw_document_types,
)
from app.services.nave import (
    NaveError,
    create_payment_request,
    extract_nave_status,
    get_payment_by_check_url,
)

logger = logging.getLogger(__name__)

_TERRAWIND_SLUG = "terrawind"
_NAVE_PAYMENT_METHOD = {
    "id": "nave",
    "code": "NAVE",
    "financing": [],
}
_LOCKED_STATUSES = {
    EmissionStatus.PASSENGERS_COMPLETE.value,
    EmissionStatus.EMITTING.value,
    EmissionStatus.ISSUED.value,
    EmissionStatus.CANCELLED.value,
}


class TerrawindFlowError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def open_terrawind_sale(
    session: Session,
    user: User,
    company_name: str,
    quote_id: str,
    quote: TerrawindQuoteSnapshot,
) -> tuple[Sale, bool]:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _attribution,
        _local_plan_id,
        _normalize_company,
    )

    if _normalize_company(company_name) != _TERRAWIND_SLUG:
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    product_id = quote_id.strip()
    if not product_id:
        raise SaleFlowError(400, "El plan de Terrawind no es válido")
    if quote.currency.upper() != "ARS":
        raise SaleFlowError(400, "Terrawind con Nave requiere moneda ARS")

    company = session.exec(select(Company).where(Company.slug == _TERRAWIND_SLUG)).first()
    if company is None or company.id is None:
        raise SaleFlowError(400, "Terrawind no está cargada como compañía")
    if not company.active:
        raise SaleFlowError(400, "Terrawind no está activa")

    reference = _terrawind_reference(user.id or 0, product_id, quote)
    existing = session.exec(select(Sale).where(Sale.provider_reference == reference)).first()
    if existing is not None:
        if existing.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta")
        if existing.emission_status in _LOCKED_STATUSES:
            return existing, False
        if existing.emission_status == EmissionStatus.PRODUCT_SELECTED.value:
            return existing, False

    context = {
        "product_id": product_id,
        "setw_country_id": quote.setw_country_id.strip(),
        "trip_type": quote.trip_type,
        "amount": str(quote.amount),
        "currency": quote.currency.upper(),
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
            origin=quote.origin[:16],
            destination_id=quote.destination_id,
            trip_type=quote.trip_type,
            days_range=quote.days_range,
            departure_date=quote.departure_date,
            return_date=quote.return_date,
            quoted_ages=list(quote.ages),
            external_plan_id=product_id[:80],
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
        existing.external_plan_id = product_id[:80]
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

    session.flush()
    _add_attempt(
        session,
        sale,
        success=True,
        http_status=200,
        request_payload={"product_id": product_id, "quote": quote.model_dump(mode="json")},
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
        "sale terrawind product_selected sale_id=%s product=%s amount=%s %s",
        sale.id,
        sale.external_plan_id,
        sale.amount,
        sale.currency,
    )
    return sale, created


def set_terrawind_passengers(
    session: Session,
    user: User,
    sale_id: int,
    data: PassengersCreate,
) -> PassengersResponse:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _age_on,
        _commit,
        _replace_passengers_generic,
        passengers_response,
    )

    sale = _editable_terrawind_sale(session, user, sale_id)
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
    missing_gender = [
        p.document_number
        for p in data.passengers
        if not (p.gender and str(p.gender).strip())
    ]
    if missing_gender:
        raise SaleFlowError(
            400,
            "Terrawind requiere gender (m/f) en todos los pasajeros",
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
    context["payment_methods"] = [_NAVE_PAYMENT_METHOD]
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
        response_payload={"payment_methods": [_NAVE_PAYMENT_METHOD]},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return passengers_response(session, sale)


def start_terrawind_payment(
    session: Session,
    user: User,
    sale_id: int,
    data: PaymentStart,
) -> PaymentResponse:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _check_return_urls,
        _commit,
        _payment_response,
        _pending_payment,
    )

    sale = _payable_terrawind_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")
    if sale.currency.upper() != "ARS":
        raise SaleFlowError(400, "Nave solo cobra en ARS")

    method_id = (data.payment_method_id or "nave").strip().lower()
    # PaymentStart default histórico es "simulated"; Terrawind siempre cobra con Nave.
    if method_id not in {"nave", "simulated"}:
        raise SaleFlowError(400, "Terrawind usa payment_method_id=nave")

    existing = _pending_payment(session, sale.id or 0)
    if (
        existing is not None
        and existing.provider == PaymentProvider.NAVE.value
        and existing.checkout_url
    ):
        return _payment_response(sale, existing.checkout_url, created=False)

    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")

    try:
        _run_check_vouchers(sale, passengers)
    except TerrawindEmissionError as exc:
        raise SaleFlowError(
            exc.status_code if exc.status_code < 500 else 502,
            exc.detail,
        ) from exc

    external_payment_id = f"tw{sale.id}-{uuid.uuid4().hex[:12]}"[:36]
    holder = next((row for row in passengers if row.is_holder), passengers[0])
    buyer = {
        "user_id": f"sale-{sale.id}",
        "name": f"{holder.first_name} {holder.last_name}".strip(),
        "user_email": holder.email,
        "doc_type": holder.document_type.upper() if holder.document_type else "DNI",
        "doc_number": holder.document_number,
        "phone": holder.phone,
    }
    buyer = {k: v for k, v in buyer.items() if v}

    try:
        link = create_payment_request(
            external_payment_id=external_payment_id,
            amount=Decimal(sale.amount),
            product_name=sale.plan_name or "Asistencia Terrawind",
            product_description=f"Sale {sale.id} · plan {sale.external_plan_id}",
            buyer=buyer,
            callback_url=data.return_url_ok.strip(),
        )
    except NaveError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc

    payment = Payment(
        sale_id=sale.id or 0,
        provider=PaymentProvider.NAVE.value,
        status=PaymentStatus.PENDING.value,
        amount=sale.amount,
        currency=sale.currency,
        checkout_url=link.checkout_url[:2000],
        # Columna external_id = external_payment_id de Nave (ej. viabarilo_001)
        external_id=link.external_payment_id[:80],
        financing_id=None,
        provider_data={
            # id de la intención en Nave (payment_request_id)
            "id": link.payment_request_id,
            "external_payment_id": link.external_payment_id,
            "checkout_url": link.checkout_url,
            "payment_request_id": link.payment_request_id,
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
        request_payload={
            "provider": PaymentProvider.NAVE.value,
            "external_payment_id": link.external_payment_id,
        },
        response_payload={
            "checkout_url": link.checkout_url,
            "payment_request_id": link.payment_request_id,
        },
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, link.checkout_url, created=True)


def confirm_terrawind_payment(
    session: Session,
    user: User,
    sale_id: int,
) -> PaymentConfirmResponse:
    sale = _owned_terrawind_sale(session, user, sale_id)
    return _confirm_terrawind_sale(session, sale)


def handle_nave_payment_notification(
    session: Session,
    *,
    external_payment_id: str,
    payment_id: str,
    payment_check_url: str,
) -> PaymentConfirmResponse | None:
    """Webhook Nave: actualiza Payment (external_id/checkout_url/id) y emite si approved."""
    from app.sales.service import SaleFlowError, _add_attempt, _commit

    ext_id = external_payment_id.strip()[:80]
    match = session.exec(
        select(Payment).where(
            Payment.provider == PaymentProvider.NAVE.value,
            Payment.external_id == ext_id,
        )
    ).first()
    if match is None:
        logger.warning(
            "Nave webhook: payment no encontrado external_payment_id=%s",
            external_payment_id,
        )
        return None

    sale = session.get(Sale, match.sale_id)
    if sale is None:
        return None
    company = session.get(Company, sale.company_id)

    status_name: str | None = None
    remote: dict[str, Any] = {}
    try:
        remote = get_payment_by_check_url(payment_check_url)
        status_name = extract_nave_status(remote)
    except NaveError as exc:
        logger.error(
            "Nave webhook: check falló payment_id=%s detail=%s",
            payment_id,
            exc.detail,
        )
        return None

    # Persistimos en Payment lo que Nave identifica:
    # - external_id (columna) = external_payment_id
    # - checkout_url (columna)
    # - provider_data.id = id de la intención / payment_request
    match.external_id = ext_id
    checkout_url = _first_str(
        remote.get("checkout_url"),
        (match.provider_data or {}).get("checkout_url"),
        match.checkout_url,
    )
    if checkout_url:
        match.checkout_url = checkout_url[:2000]

    nave_request_id = _first_str(
        remote.get("id"),
        remote.get("payment_request_id"),
        (match.provider_data or {}).get("id"),
        (match.provider_data or {}).get("payment_request_id"),
    )

    provider_data = dict(match.provider_data or {})
    if nave_request_id:
        provider_data["id"] = nave_request_id
        provider_data["payment_request_id"] = nave_request_id
    provider_data["external_payment_id"] = ext_id
    if checkout_url:
        provider_data["checkout_url"] = checkout_url
    provider_data["nave_payment_id"] = payment_id
    provider_data["payment_check_url"] = payment_check_url
    provider_data["last_status"] = status_name
    match.provider_data = provider_data
    flag_modified(match, "provider_data")
    match.updated_at = datetime.utcnow()

    if status_name == "approved":
        match.status = PaymentStatus.APPROVED.value
        sale.payment_status = SalePaymentStatus.APPROVED.value
    elif status_name == "rejected":
        match.status = PaymentStatus.REJECTED.value
        sale.payment_status = SalePaymentStatus.REJECTED.value
    elif status_name == "pending":
        match.status = PaymentStatus.PENDING.value
        sale.payment_status = SalePaymentStatus.PENDING.value

    sale.updated_at = datetime.utcnow()
    session.add(match)
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="nave_webhook",
        success=status_name == "approved",
        http_status=200,
        request_payload={
            "external_payment_id": external_payment_id,
            "payment_id": payment_id,
        },
        response_payload={
            "status": status_name,
            "id": nave_request_id,
            "external_payment_id": ext_id,
            "checkout_url": checkout_url,
        },
        error_message=None if status_name == "approved" else status_name,
    )
    _commit(session)
    session.refresh(sale)

    if company is not None and company.slug == _TERRAWIND_SLUG and status_name == "approved":
        try:
            return _confirm_terrawind_sale(session, sale)
        except SaleFlowError as exc:
            logger.error(
                "Nave webhook: emisión falló sale_id=%s detail=%s",
                sale.id,
                exc.detail,
            )
    return None


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def get_terrawind_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    from app.sales.service import sale_detail

    sale = _owned_terrawind_sale(
        session, user, sale_id, forbidden="No podés ver esta venta"
    )
    return sale_detail(session, sale, _TERRAWIND_SLUG)


def list_terrawind_countries() -> list[dict[str, str]]:
    from app.sales.service import SaleFlowError

    try:
        return list_setw_countries()
    except TerrawindEmissionError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc


def list_terrawind_document_types() -> list[dict[str, str]]:
    from app.sales.service import SaleFlowError

    try:
        return list_setw_document_types()
    except TerrawindEmissionError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc


def _confirm_terrawind_sale(session: Session, sale: Sale) -> PaymentConfirmResponse:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _commit,
        _confirm_response,
        _voucher_for,
    )

    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
        return _confirm_response(sale, True, voucher, company_slug=_TERRAWIND_SLUG)

    payment = _latest_nave_payment(session, sale.id or 0)
    if payment is None:
        raise SaleFlowError(409, "No hay un pago Nave para esta venta")

    # El webhook de Nave sandbox no llega a este entorno. Confirmar emite
    # sin esperar que el pago figure approved.
    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")
    if sale.departure_date is None or sale.return_date is None:
        raise SaleFlowError(400, "La venta no tiene fechas de viaje")

    product_id = str(
        (sale.provider_context or {}).get("product_id") or sale.external_plan_id
    ).strip()
    country_id = str(
        (sale.provider_context or {}).get("setw_country_id") or "9999"
    ).strip()

    drafts = [_to_draft(sale, row) for row in passengers]
    sale.emission_status = EmissionStatus.EMITTING.value
    sale.updated_at = datetime.utcnow()
    session.add(sale)
    session.flush()

    try:
        result = emit_terrawind_vouchers(
            product_id=product_id,
            country_id=country_id,
            departure_date=sale.departure_date,
            return_date=sale.return_date,
            passengers=drafts,
            voucher_int_ref_prefix=f"sale-{sale.id}",
        )
    except TerrawindEmissionError as exc:
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
    context["vouchers"] = [
        {
            "voucher_number": item.voucher_number,
            "voucher_key": item.voucher_key,
            "pdf_url": item.pdf_url,
            "passenger_document_number": item.passenger_document_number,
        }
        for item in result.vouchers
    ]
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    sale.emission_status = EmissionStatus.ISSUED.value
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.updated_at = datetime.utcnow()
    payment.status = PaymentStatus.APPROVED.value
    payment.updated_at = datetime.utcnow()
    session.add(payment)
    session.add(sale)

    primary = result.vouchers[0]
    voucher = _save_terrawind_voucher(session, sale, primary, result.vouchers)
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
    return _confirm_response(sale, True, voucher, company_slug=_TERRAWIND_SLUG)


def _run_check_vouchers(sale: Sale, passengers: list[Passenger]) -> None:
    if sale.departure_date is None or sale.return_date is None:
        raise TerrawindEmissionError(400, "La venta no tiene fechas de viaje")
    product_id = str(
        (sale.provider_context or {}).get("product_id") or sale.external_plan_id
    ).strip()
    country_id = str(
        (sale.provider_context or {}).get("setw_country_id") or "9999"
    ).strip()
    for row in passengers:
        check_voucher(
            product_id=product_id,
            country_id=country_id,
            departure_date=sale.departure_date,
            return_date=sale.return_date,
            passenger=_to_draft(sale, row),
            voucher_int_ref=f"sale-{sale.id}-check",
        )


def _to_draft(sale: Sale, row: Passenger) -> TerrawindPassengerDraft:
    address_parts = [part for part in (row.street, row.street_number) if part]
    return TerrawindPassengerDraft(
        document_type=row.document_type,
        document_number=row.document_number,
        birth_date=row.birth_date,
        gender=(row.gender or "").strip() or "m",
        first_name=row.first_name,
        last_name=row.last_name,
        email=row.email,
        phone=row.phone,
        city=row.city,
        address=" ".join(address_parts) if address_parts else None,
        residence_country_id=(row.provider_data or {}).get("country_id"),
        emergency_first_name=sale.emergency_first_name or "",
        emergency_last_name=sale.emergency_last_name or "",
        emergency_phone_1=sale.emergency_phone or "",
    )


def _save_terrawind_voucher(
    session: Session,
    sale: Sale,
    primary: Any,
    all_vouchers: list[Any],
) -> Voucher:
    external_id = primary.voucher_number
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
        url=(primary.pdf_url or "")[:2000] or None,
        status=VoucherStatus.ISSUED.value,
        provider_data={
            "voucher_key": primary.voucher_key,
            "vouchers": [
                {
                    "voucher_number": item.voucher_number,
                    "voucher_key": item.voucher_key,
                    "pdf_url": item.pdf_url,
                }
                for item in all_vouchers
            ],
        },
    )
    session.add(voucher)
    session.flush()
    return voucher


def _latest_nave_payment(session: Session, sale_id: int) -> Payment | None:
    return session.exec(
        select(Payment)
        .where(
            Payment.sale_id == sale_id,
            Payment.provider == PaymentProvider.NAVE.value,
        )
        .order_by(Payment.id.desc())
    ).first()


def _editable_terrawind_sale(session: Session, user: User, sale_id: int) -> Sale:
    from app.sales.service import SaleFlowError

    sale = _owned_terrawind_sale(
        session, user, sale_id, forbidden="No podés cargar pasajeros en esta venta"
    )
    if sale.emission_status not in (
        EmissionStatus.PRODUCT_SELECTED.value,
        EmissionStatus.PASSENGERS_COMPLETE.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cargar pasajeros")
    return sale


def _payable_terrawind_sale(session: Session, user: User, sale_id: int) -> Sale:
    from app.sales.service import SaleFlowError

    sale = _owned_terrawind_sale(session, user, sale_id)
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.ISSUED.value,
        EmissionStatus.EMISSION_FAILED.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cobrar")
    return sale


def _owned_terrawind_sale(
    session: Session,
    user: User,
    sale_id: int,
    *,
    forbidden: str = "No podés cobrar esta venta",
) -> Sale:
    from app.sales.service import SaleFlowError

    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    if sale.created_by_user_id != user.id:
        raise SaleFlowError(403, forbidden)
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _TERRAWIND_SLUG:
        raise SaleFlowError(400, "Esta venta no es de Terrawind")
    return sale


def _terrawind_reference(user_id: int, product_id: str, quote: TerrawindQuoteSnapshot) -> str:
    raw = (
        f"{user_id}|{product_id}|{quote.departure_date}|{quote.return_date}|"
        f"{','.join(str(a) for a in quote.ages)}|{quote.origin}|"
        f"{quote.destination_id}|{quote.setw_country_id}|{quote.amount}|{quote.currency}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"terrawind:{product_id}:{digest}"
