"""Flujo de venta New Travel: snapshot local, emisión waitForPayment y paymentLink."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

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
    NewTravelQuoteSnapshot,
    PassengersCreate,
    PassengersResponse,
    PaymentConfirmResponse,
    PaymentResponse,
    PaymentStart,
    SaleDetailResponse,
)
from app.services.companies.new_travel_emission import (
    NewTravelContactDraft,
    NewTravelEmissionError,
    NewTravelPassengerDraft,
    create_order_pending_payment,
    create_payment_link,
    build_voucher_print_url,
    get_order,
    list_new_travel_countries,
    list_new_travel_document_types,
    send_voucher,
    territory_for_destination,
)

logger = logging.getLogger(__name__)

_NEW_TRAVEL_SLUG = "new_travel"
_PAYMENT_METHOD = {
    "id": "new_travel",
    "code": "NEW_TRAVEL",
    "financing": [],
}
_LOCKED_STATUSES = {
    EmissionStatus.PASSENGERS_COMPLETE.value,
    EmissionStatus.EMITTING.value,
    EmissionStatus.ISSUED.value,
    EmissionStatus.CANCELLED.value,
}


def open_new_travel_sale(
    session: Session,
    user: User,
    company_name: str,
    quote_id: str,
    quote: NewTravelQuoteSnapshot,
) -> tuple[Sale, bool]:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _attribution,
        _local_plan_id,
        _normalize_company,
    )

    if _normalize_company(company_name) != _NEW_TRAVEL_SLUG:
        raise SaleFlowError(400, "La emisión para esta compañía todavía no está disponible")
    plan_id = quote_id.strip()
    if not plan_id:
        raise SaleFlowError(400, "El plan de New Travel no es válido")
    if quote.trip_type != "multiviaje" and quote.return_date is None:
        raise SaleFlowError(400, "Falta la fecha de regreso")
    if quote.trip_type == "multiviaje" and quote.days_range is None:
        raise SaleFlowError(400, "Multiviaje requiere days_range")

    try:
        territory_for_destination(quote.destination_id)
    except NewTravelEmissionError as exc:
        raise SaleFlowError(exc.status_code, exc.detail) from exc

    company = session.exec(select(Company).where(Company.slug == _NEW_TRAVEL_SLUG)).first()
    if company is None or company.id is None:
        raise SaleFlowError(400, "New Travel no está cargada como compañía")
    if not company.active:
        raise SaleFlowError(400, "New Travel no está activa")

    reference = _new_travel_reference(user.id or 0, plan_id, quote)
    existing = session.exec(select(Sale).where(Sale.provider_reference == reference)).first()
    if existing is not None:
        if existing.created_by_user_id != user.id:
            raise SaleFlowError(409, "Esa cotización ya tiene una venta")
        if existing.emission_status in _LOCKED_STATUSES:
            return existing, False
        if existing.emission_status == EmissionStatus.PRODUCT_SELECTED.value:
            return existing, False

    context = {
        "plan_id": plan_id,
        "trip_type": quote.trip_type,
        "amount": str(quote.amount),
        "currency": quote.currency.upper(),
        "territory_id": territory_for_destination(quote.destination_id),
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
        "sale new_travel product_selected sale_id=%s plan=%s amount=%s %s",
        sale.id,
        sale.external_plan_id,
        sale.amount,
        sale.currency,
    )
    return sale, created


def set_new_travel_passengers(
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

    sale = _editable_new_travel_sale(session, user, sale_id)
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
    missing_contact = [
        p.document_number
        for p in data.passengers
        if not (p.email and str(p.email).strip() and p.phone and str(p.phone).strip())
    ]
    if missing_contact:
        raise SaleFlowError(
            400,
            "New Travel requiere email y teléfono en todos los pasajeros",
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
    context["payment_methods"] = [_PAYMENT_METHOD]
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
        response_payload={"payment_methods": [_PAYMENT_METHOD]},
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return passengers_response(session, sale)


def start_new_travel_payment(
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

    sale = _payable_new_travel_sale(session, user, sale_id)
    _check_return_urls(data)
    if sale.emission_status == EmissionStatus.ISSUED.value:
        raise SaleFlowError(409, "Esta venta ya está emitida")

    method_id = (data.payment_method_id or "new_travel").strip().lower()
    if method_id not in {"new_travel", "simulated"}:
        raise SaleFlowError(400, "New Travel usa payment_method_id=new_travel")

    existing = _pending_payment(session, sale.id or 0)
    if (
        existing is not None
        and existing.provider == PaymentProvider.NEW_TRAVEL_GATEWAY.value
        and existing.checkout_url
    ):
        return _payment_response(sale, existing.checkout_url, created=False)

    passengers = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    if not passengers:
        raise SaleFlowError(400, "La venta no tiene pasajeros")
    if sale.departure_date is None or sale.return_date is None:
        raise SaleFlowError(400, "La venta no tiene fechas de viaje")

    context = dict(sale.provider_context or {})
    order_codes = context.get("voucher_codes")
    primary_code = None
    if isinstance(order_codes, list) and order_codes:
        primary_code = str(order_codes[0]).strip() or None

    try:
        if not primary_code:
            created = _create_pending_order(sale, passengers)
            primary_code = created.voucher_codes[0]
            context["voucher_codes"] = created.voucher_codes
            context["order_total"] = created.total
            context["order_status"] = created.order_status
            sale.provider_context = context
            flag_modified(sale, "provider_context")
            session.add(sale)
            _add_attempt(
                session,
                sale,
                step="create_order",
                success=True,
                http_status=created.http_status,
                request_payload=created.request_body,
                response_payload=created.response_body,
                error_message=None,
            )

        holder = next((row for row in passengers if row.is_holder), passengers[0])
        link_email = holder.email or sale.emergency_email
        link = create_payment_link(primary_code, email=link_email)
    except NewTravelEmissionError as exc:
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
        raise SaleFlowError(
            exc.status_code if exc.status_code < 500 else 502,
            exc.detail,
        ) from exc

    context = dict(sale.provider_context or {})
    context["payment_link"] = {
        "url": link.url,
        "status": link.status,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "code": link.code,
    }
    sale.provider_context = context
    flag_modified(sale, "provider_context")

    payment = Payment(
        sale_id=sale.id or 0,
        provider=PaymentProvider.NEW_TRAVEL_GATEWAY.value,
        status=PaymentStatus.PENDING.value,
        amount=sale.amount,
        currency=sale.currency,
        checkout_url=link.url[:2000],
        external_id=link.code[:80],
        financing_id=None,
        provider_data={
            "voucher_code": link.code,
            "payment_link": link.url,
            "payment_link_status": link.status,
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
        http_status=link.http_status,
        request_payload=link.request_body,
        response_payload=link.response_body,
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _payment_response(sale, link.url, created=True)


def confirm_new_travel_payment(
    session: Session,
    user: User,
    sale_id: int,
) -> PaymentConfirmResponse:
    sale = _owned_new_travel_sale(session, user, sale_id)
    return _confirm_new_travel_sale(session, sale)


def get_new_travel_sale(session: Session, user: User, sale_id: int) -> SaleDetailResponse:
    from app.sales.service import sale_detail

    sale = _owned_new_travel_sale(
        session, user, sale_id, forbidden="No podés ver esta venta"
    )
    return sale_detail(session, sale, _NEW_TRAVEL_SLUG)


def list_document_types() -> list[dict[str, str]]:
    return list_new_travel_document_types()


def list_countries() -> list[dict[str, str]]:
    try:
        return list_new_travel_countries()
    except NewTravelEmissionError as exc:
        from app.sales.service import SaleFlowError

        raise SaleFlowError(
            exc.status_code if exc.status_code < 500 else 502,
            exc.detail,
        ) from exc


def list_provinces(country_id: str) -> list[dict[str, str]]:
    """Provincias solo para el formulario local (igual Inter Assist).

    New Travel no pide provincia en la emisión. Para AR usamos Georef;
    otros países: lista vacía.
    """
    from app.services.companies.inter_assist_catalog import list_inter_assist_provinces

    return list_inter_assist_provinces(country_id)


def _confirm_new_travel_sale(session: Session, sale: Sale) -> PaymentConfirmResponse:
    from app.sales.service import (
        SaleFlowError,
        _add_attempt,
        _commit,
        _confirm_response,
        _pending_payment,
        _voucher_for,
    )

    if sale.emission_status == EmissionStatus.ISSUED.value:
        voucher = _voucher_for(session, sale.id or 0)
        if voucher is not None and not voucher.url:
            code = (voucher.external_id or voucher.number or "").strip()
            if not code:
                codes = (sale.provider_context or {}).get("voucher_codes") or []
                code = str(codes[0]).strip() if codes else ""
            print_url = build_voucher_print_url(code)
            if print_url:
                voucher.url = print_url[:2000]
                session.add(voucher)
                _commit(session)
                session.refresh(voucher)
        return _confirm_response(sale, True, voucher)

    if sale.payment_status != SalePaymentStatus.PENDING.value:
        raise SaleFlowError(409, "El pago de esta venta no está iniciado")

    codes = (sale.provider_context or {}).get("voucher_codes") or []
    if not isinstance(codes, list) or not codes:
        payment = _pending_payment(session, sale.id or 0)
        if payment and payment.external_id:
            codes = [payment.external_id]
    if not codes:
        raise SaleFlowError(400, "La venta no tiene código de orden New Travel")

    primary = str(codes[0]).strip()
    try:
        order = get_order(primary)
    except NewTravelEmissionError as exc:
        _add_attempt(
            session,
            sale,
            step="confirm_payment",
            success=False,
            http_status=exc.http_status or exc.status_code,
            request_payload={"code": primary},
            response_payload=exc.response_body or {"detail": exc.detail},
            error_message=exc.detail,
        )
        _commit(session)
        raise SaleFlowError(502, exc.detail) from exc

    if not order.paid:
        _add_attempt(
            session,
            sale,
            step="confirm_payment",
            success=True,
            http_status=order.http_status,
            request_payload={"code": primary},
            response_payload=order.response_body,
            error_message="El pago todavía no está acreditado",
        )
        _commit(session)
        return _confirm_response(sale, False, None)

    voucher_url = order.voucher_url or build_voucher_print_url(primary)
    rows = session.exec(
        select(Passenger).where(Passenger.sale_id == sale.id).order_by(Passenger.id)
    ).all()
    holder = next((row for row in rows if row.is_holder), rows[0] if rows else None)
    email = (holder.email if holder else None) or sale.emergency_email
    if email:
        try:
            send_voucher(primary, email)
        except NewTravelEmissionError as exc:
            logger.warning(
                "new_travel sendVoucher failed sale_id=%s detail=%s",
                sale.id,
                exc.detail,
            )

    voucher = _save_new_travel_voucher(
        session,
        sale,
        codes=[str(c) for c in codes],
        voucher_url=voucher_url,
    )
    payment = _pending_payment(session, sale.id or 0)
    if payment is not None:
        payment.status = PaymentStatus.APPROVED.value
        payment.updated_at = datetime.utcnow()
        session.add(payment)
    sale.payment_status = SalePaymentStatus.APPROVED.value
    sale.emission_status = EmissionStatus.ISSUED.value
    sale.updated_at = datetime.utcnow()
    context = dict(sale.provider_context or {})
    context["order_paid"] = True
    if order.payment_link_status:
        link = dict(context.get("payment_link") or {})
        link["status"] = order.payment_link_status
        context["payment_link"] = link
    sale.provider_context = context
    flag_modified(sale, "provider_context")
    session.add(sale)
    _add_attempt(
        session,
        sale,
        step="confirm_payment",
        success=True,
        http_status=order.http_status,
        request_payload={"code": primary},
        response_payload=order.response_body,
        error_message=None,
    )
    _commit(session)
    session.refresh(sale)
    return _confirm_response(sale, True, voucher)


def _create_pending_order(sale: Sale, passengers: list[Passenger]):
    holder = next((row for row in passengers if row.is_holder), passengers[0])
    contact_name = " ".join(
        part
        for part in (
            sale.emergency_first_name,
            sale.emergency_last_name,
        )
        if part
    ).strip()
    if not contact_name:
        contact_name = f"{holder.first_name} {holder.last_name}".strip()
    contact_email = sale.emergency_email or holder.email
    contact_phone = sale.emergency_phone or holder.phone
    if not contact_email or not contact_phone:
        raise NewTravelEmissionError(
            400,
            "Falta email/teléfono de contacto para New Travel",
        )

    origin = (sale.origin or "AR").strip().upper()[:2]
    territory = territory_for_destination(sale.destination_id)
    drafts = [_passenger_draft(row, default_country=origin) for row in passengers]
    contact = NewTravelContactDraft(
        name=contact_name,
        email=str(contact_email),
        phone=str(contact_phone),
        code_phone=_code_phone_for(origin),
    )
    days_range = sale.days_range if sale.trip_type == "multiviaje" else None
    assert sale.departure_date is not None and sale.return_date is not None
    return create_order_pending_payment(
        plan_id=sale.external_plan_id,
        origin=origin,
        destination_territory=territory,
        start_date=sale.departure_date,
        end_date=sale.return_date,
        contact=contact,
        beneficiaries=drafts,
        days_range=days_range,
        individual_voucher=True,
        reference=f"sale-{sale.id}",
    )


def _passenger_draft(row: Passenger, *, default_country: str) -> NewTravelPassengerDraft:
    country = default_country
    provider = row.provider_data or {}
    country_id = provider.get("country_id")
    if isinstance(country_id, str) and len(country_id.strip()) == 2:
        country = country_id.strip().upper()
    if not row.email or not row.phone:
        raise NewTravelEmissionError(
            400,
            "New Travel requiere email y teléfono en todos los pasajeros",
        )
    return NewTravelPassengerDraft(
        first_name=row.first_name,
        last_name=row.last_name,
        birth_date=row.birth_date,
        document_type=row.document_type,
        document_number=row.document_number,
        email=row.email,
        phone=row.phone,
        country=country,
        code_phone=_code_phone_for(country),
        sex=(row.gender or None),
    )


def _code_phone_for(country_iso: str) -> str:
    if country_iso.upper() == "AR":
        return "54"
    return "54"


def _save_new_travel_voucher(
    session: Session,
    sale: Sale,
    *,
    codes: list[str],
    voucher_url: str | None,
) -> Voucher:
    primary = (codes[0] if codes else "")[:80]
    existing = session.exec(
        select(Voucher).where(
            Voucher.sale_id == sale.id,
            Voucher.external_id == primary,
        )
    ).first()
    if existing is not None:
        if voucher_url and not existing.url:
            existing.url = voucher_url[:2000]
            session.add(existing)
            session.flush()
        return existing
    voucher = Voucher(
        sale_id=sale.id or 0,
        passenger_id=None,
        external_id=primary,
        number=primary or None,
        url=(voucher_url[:2000] if voucher_url else None),
        status=VoucherStatus.ISSUED.value,
        provider_data={"voucher_codes": codes},
    )
    session.add(voucher)
    session.flush()
    return voucher


def _editable_new_travel_sale(session: Session, user: User, sale_id: int) -> Sale:
    from app.sales.service import SaleFlowError

    sale = _owned_new_travel_sale(
        session, user, sale_id, forbidden="No podés cargar pasajeros en esta venta"
    )
    if sale.emission_status not in {
        EmissionStatus.PRODUCT_SELECTED.value,
        EmissionStatus.PASSENGERS_COMPLETE.value,
    }:
        raise SaleFlowError(409, "Esta venta ya no admite cargar pasajeros")
    if sale.payment_status not in {
        SalePaymentStatus.NOT_STARTED.value,
        SalePaymentStatus.REJECTED.value,
    }:
        raise SaleFlowError(409, "Esta venta ya tiene un pago en curso")
    return sale


def _payable_new_travel_sale(session: Session, user: User, sale_id: int) -> Sale:
    from app.sales.service import SaleFlowError

    sale = _owned_new_travel_sale(session, user, sale_id)
    if sale.emission_status not in (
        EmissionStatus.PASSENGERS_COMPLETE.value,
        EmissionStatus.ISSUED.value,
    ):
        raise SaleFlowError(409, "Esta venta no está lista para cobrar")
    return sale


def _owned_new_travel_sale(
    session: Session,
    user: User,
    sale_id: int,
    *,
    forbidden: str = "No podés cobrar esta venta",
) -> Sale:
    from app.sales.service import SaleFlowError
    from app.models.user import UserRole

    sale = session.get(Sale, sale_id)
    if sale is None:
        raise SaleFlowError(404, "No encontramos esa venta")
    owner = sale.created_by_user_id == user.id
    admin_reader = user.role == UserRole.ADMIN and forbidden == "No podés ver esta venta"
    if not owner and not admin_reader:
        raise SaleFlowError(403, forbidden)
    company = session.get(Company, sale.company_id)
    if company is None or company.slug != _NEW_TRAVEL_SLUG:
        raise SaleFlowError(400, "Esta venta no es de New Travel")
    return sale


def _new_travel_reference(user_id: int, plan_id: str, quote: NewTravelQuoteSnapshot) -> str:
    raw = "|".join(
        [
            str(user_id),
            plan_id,
            quote.departure_date.isoformat(),
            (quote.return_date.isoformat() if quote.return_date else ""),
            ",".join(str(a) for a in quote.ages),
            quote.origin,
            str(quote.destination_id),
            quote.trip_type,
            str(quote.days_range or ""),
            str(quote.amount),
            quote.currency.upper(),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"new_travel:{plan_id}:{digest}"
