"""Emisión y checkout de InterAssist.

`emitir-externa` crea la venta sin cobrar. El cobro del pasajero va por
`/api/ventas/checkout/mercadopago` o `/api/ventas/checkout/payway`: la venta
y los vouchers aparecen en el GET de la orden recién cuando el pago está aprobado.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.quotations.schemas import (
    TRIP_TYPE_LARGA_ESTADIA,
    TRIP_TYPE_MULTIVIAJE,
    TRIP_TYPE_UNICO_VIAJE,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 60.0
_PERIOD_BY_DAYS: dict[int, int] = {30: 1, 60: 2, 90: 3}
_TYPE_BY_TRIP: dict[str, int] = {
    TRIP_TYPE_UNICO_VIAJE: 0,
    TRIP_TYPE_MULTIVIAJE: 1,
    TRIP_TYPE_LARGA_ESTADIA: 2,
}


class InterAssistEmissionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status: int | None = None
        self.request_body: dict[str, Any] | None = None
        self.response_body: dict[str, Any] | None = None


@dataclass
class InterAssistClientDraft:
    first_name: str
    last_name: str
    birth_date: date
    document_number: str
    email: str | None = None
    phone: str | None = None


@dataclass
class InterAssistEmissionResult:
    venta_id: int
    voucher_ids: list[int]
    status_code: int
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)
    warnings: list[Any] = field(default_factory=list)


@dataclass
class PaywayCard:
    id: int
    name: str


@dataclass
class PaywayCardsResult:
    cards: list[PaywayCard]
    max_installments: int


@dataclass
class InterAssistCheckoutResult:
    order_uuid: str
    status: str
    checkout_url: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    exchange_rate: Decimal | None = None
    gateway: str | None = None
    venta_id: int | None = None
    voucher_ids: list[int] = field(default_factory=list)
    status_code: int = 200
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterAssistVoucherInfo:
    """Voucher de Inter Assist con el viajero (GET /api/ventas/{id} o /api/vouchers/{id})."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    document_number: str | None = None
    email: str | None = None


_CHECKOUT_GATEWAYS = frozenset({"mercadopago", "payway"})


def emission_type_and_periodo(
    trip_type: str,
    days_range: int | None,
) -> tuple[int, int | None]:
    """Mapea trip_type del cotizador a type/periodo de InterAssist."""
    key = trip_type.strip().lower()
    if key not in _TYPE_BY_TRIP:
        raise InterAssistEmissionError(400, "Tipo de viaje no soportado para Inter Assist")
    emission_type = _TYPE_BY_TRIP[key]
    if emission_type != 1:
        return emission_type, None
    if days_range is None or days_range not in _PERIOD_BY_DAYS:
        raise InterAssistEmissionError(
            400,
            "Multiviaje requiere days_range 30, 60 o 90",
        )
    return emission_type, _PERIOD_BY_DAYS[days_range]


def format_interassist_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def build_emitir_externa_payload(
    *,
    plan_id: int,
    trip_type: str,
    days_range: int | None,
    departure_date: date,
    return_date: date | None,
    clientes: list[InterAssistClientDraft],
    promocion_id: int = 0,
) -> dict[str, Any]:
    emission_type, periodo = emission_type_and_periodo(trip_type, days_range)
    body: dict[str, Any] = {
        "type": emission_type,
        "plan_id": plan_id,
        "promocion_id": promocion_id,
        "data_inicial": format_interassist_date(departure_date),
        "clientes": [_cliente_dict(cliente) for cliente in clientes],
    }
    if emission_type in (0, 2):
        if return_date is None:
            raise InterAssistEmissionError(400, "Falta la fecha de regreso")
        body["data_final"] = format_interassist_date(return_date)
    if emission_type == 1:
        body["periodo"] = periodo
        if return_date is not None:
            body["data_final"] = format_interassist_date(return_date)
    return body


def build_checkout_payload(
    *,
    gateway: str,
    plan_id: int,
    trip_type: str,
    days_range: int | None,
    departure_date: date,
    return_date: date | None,
    clientes: list[InterAssistClientDraft],
    promocion_id: int = 0,
    success_url: str,
    failure_url: str,
    pending_url: str | None = None,
    notification_url: str | None = None,
    credit_card_id: int | None = None,
    installments: int | None = None,
) -> dict[str, Any]:
    """Body de emitir-externa más las URLs de retorno.

    Mercado Pago exige pending_url y no lleva tarjeta ni cuotas.
    Payway exige credit_card_id e installments. pending_url es opcional.
    """
    key = gateway.strip().lower()
    if key not in _CHECKOUT_GATEWAYS:
        raise InterAssistEmissionError(400, "La pasarela tiene que ser mercadopago o payway")
    body = build_emitir_externa_payload(
        plan_id=plan_id,
        trip_type=trip_type,
        days_range=days_range,
        departure_date=departure_date,
        return_date=return_date,
        clientes=clientes,
        promocion_id=promocion_id,
    )
    body["success_url"] = success_url.strip()
    body["failure_url"] = failure_url.strip()
    if key == "mercadopago":
        if not (pending_url or "").strip():
            raise InterAssistEmissionError(400, "Mercado Pago requiere pending_url")
        body["pending_url"] = pending_url.strip()
    elif (pending_url or "").strip():
        body["pending_url"] = pending_url.strip()
    if notification_url and notification_url.strip():
        body["notification_url"] = notification_url.strip()
    if key == "payway":
        if credit_card_id is None or installments is None:
            raise InterAssistEmissionError(400, "Payway requiere tarjeta y cuotas")
        body["credit_card_id"] = credit_card_id
        body["installments"] = installments
    return body


def list_payway_cards(*, client: httpx.Client | None = None) -> PaywayCardsResult:
    """Tarjetas y tope de cuotas de la agencia (GET /checkout/payway/tarjetas)."""
    settings = get_settings()
    _require_api_key(settings)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        payload, _status = _request_json(
            client,
            settings,
            "GET",
            _payway_cards_url(settings),
        )
    finally:
        if owns_client:
            client.close()

    raw_cards = payload.get("data") if isinstance(payload, dict) else None
    cards: list[PaywayCard] = []
    if isinstance(raw_cards, list):
        for item in raw_cards:
            if not isinstance(item, dict):
                continue
            card_id = _as_int(item.get("id"))
            name = str(item.get("nombre") or item.get("name") or "").strip()
            if card_id is None or not name:
                continue
            cards.append(PaywayCard(id=card_id, name=name))
    max_installments = _as_int(payload.get("max_installments") if isinstance(payload, dict) else None)
    # Doc Inter §6.3: si la empresa no cargó tope, el del servidor es 12.
    if max_installments is None or max_installments < 1:
        max_installments = 12
    return PaywayCardsResult(cards=cards, max_installments=max_installments)


def create_inter_assist_checkout(
    *,
    gateway: str,
    plan_id: int,
    trip_type: str,
    days_range: int | None,
    departure_date: date,
    return_date: date | None,
    clientes: list[InterAssistClientDraft],
    promocion_id: int = 0,
    success_url: str,
    failure_url: str,
    pending_url: str | None = None,
    notification_url: str | None = None,
    credit_card_id: int | None = None,
    installments: int | None = None,
    client: httpx.Client | None = None,
) -> InterAssistCheckoutResult:
    """Abre el checkout. No crea la venta: devuelve order_uuid y checkout_url."""
    settings = get_settings()
    _require_api_key(settings)
    key = gateway.strip().lower()
    body = build_checkout_payload(
        gateway=key,
        plan_id=plan_id,
        trip_type=trip_type,
        days_range=days_range,
        departure_date=departure_date,
        return_date=return_date,
        clientes=clientes,
        promocion_id=promocion_id,
        success_url=success_url,
        failure_url=failure_url,
        pending_url=pending_url,
        notification_url=notification_url,
        credit_card_id=credit_card_id,
        installments=installments,
    )
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        payload, status_code = _request_json(
            client,
            settings,
            "POST",
            _checkout_gateway_url(settings, key),
            json_body=body,
        )
    finally:
        if owns_client:
            client.close()
    result = _parse_checkout(payload, status_code, body, require_url=True)
    if result.gateway is None:
        result.gateway = key
    return result


def get_inter_assist_checkout(
    order_uuid: str,
    *,
    client: httpx.Client | None = None,
) -> InterAssistCheckoutResult:
    """Estado de la orden. Con pago aprobado incluye venta.id y voucher_ids."""
    settings = get_settings()
    _require_api_key(settings)
    uuid = order_uuid.strip()
    if not uuid or "/" in uuid:
        raise InterAssistEmissionError(400, "La orden de checkout no es válida")
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        payload, status_code = _request_json(
            client,
            settings,
            "GET",
            _checkout_order_url(settings, uuid),
        )
    finally:
        if owns_client:
            client.close()
    return _parse_checkout(payload, status_code, {"order_uuid": uuid}, require_url=False)


def get_inter_assist_venta_vouchers(
    venta_id: int,
    *,
    client: httpx.Client | None = None,
) -> list[InterAssistVoucherInfo]:
    """Detalle de la venta (GET /api/ventas/{id}): vouchers con cliente.

    Doc §7.4: persistir cada voucher_id. El detalle asocia cada uno al viajero.
    """
    settings = get_settings()
    _require_api_key(settings)
    if venta_id <= 0:
        raise InterAssistEmissionError(400, "La venta de Inter Assist no es válida")
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        payload, _status = _request_json(
            client,
            settings,
            "GET",
            _venta_url(settings, venta_id),
        )
    finally:
        if owns_client:
            client.close()
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raw = payload if isinstance(payload, dict) else {}
    vouchers = raw.get("vouchers") if isinstance(raw, dict) else None
    if not isinstance(vouchers, list):
        return []
    result: list[InterAssistVoucherInfo] = []
    for item in vouchers:
        if not isinstance(item, dict):
            continue
        vid = _as_int(item.get("id"))
        if vid is None or vid <= 0:
            continue
        cliente = item.get("cliente") if isinstance(item.get("cliente"), dict) else {}
        result.append(
            InterAssistVoucherInfo(
                id=vid,
                first_name=str(cliente.get("nombre") or "").strip() or None,
                last_name=str(cliente.get("apellido") or "").strip() or None,
                document_number=str(
                    cliente.get("dnipasaporte") or cliente.get("dni") or ""
                ).strip()
                or None,
                email=str(cliente.get("email") or "").strip() or None,
            )
        )
    return result


def _require_api_key(settings: Settings) -> None:
    if not settings.interassist_api_key.strip():
        raise InterAssistEmissionError(400, "Falta INTERASSIST_API_KEY")


def emit_inter_assist_sale(
    *,
    plan_id: int,
    trip_type: str,
    days_range: int | None,
    departure_date: date,
    return_date: date | None,
    clientes: list[InterAssistClientDraft],
    promocion_id: int = 0,
    client: httpx.Client | None = None,
) -> InterAssistEmissionResult:
    """Crea la venta en InterAssist y devuelve venta.id + voucher_ids."""
    settings = get_settings()
    _require_api_key(settings)

    body = build_emitir_externa_payload(
        plan_id=plan_id,
        trip_type=trip_type,
        days_range=days_range,
        departure_date=departure_date,
        return_date=return_date,
        clientes=clientes,
        promocion_id=promocion_id,
    )
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        response_body, status_code = _request_json(
            client,
            settings,
            "POST",
            _emitir_url(settings),
            json_body=body,
        )
    finally:
        if owns_client:
            client.close()

    venta = response_body.get("venta") if isinstance(response_body, dict) else None
    if not isinstance(venta, dict):
        error = InterAssistEmissionError(502, "Inter Assist no devolvió la venta emitida")
        error.http_status = status_code
        error.request_body = body
        error.response_body = response_body if isinstance(response_body, dict) else {}
        raise error

    venta_id = _as_int(venta.get("id"))
    voucher_ids = _as_int_list(venta.get("voucher_ids"))
    if venta_id is None or not voucher_ids:
        error = InterAssistEmissionError(502, "Inter Assist no devolvió voucher_ids")
        error.http_status = status_code
        error.request_body = body
        error.response_body = response_body if isinstance(response_body, dict) else {}
        raise error

    warnings = response_body.get("warnings") if isinstance(response_body, dict) else None
    return InterAssistEmissionResult(
        venta_id=venta_id,
        voucher_ids=voucher_ids,
        status_code=status_code,
        request_body=body,
        response_body=response_body if isinstance(response_body, dict) else {},
        warnings=warnings if isinstance(warnings, list) else [],
    )


def voucher_pdf_url(voucher_id: int, lang: str = "es") -> str:
    """URL externa Inter Assist. Requiere API Key: no abrirla en el navegador."""
    settings = get_settings()
    base = settings.interassist_base_url.rstrip("/")
    return f"{base}/api/vouchers/{voucher_id}/pdf/{lang}"


def fetch_voucher_pdf(voucher_id: int, lang: str = "es") -> bytes:
    """Descarga el PDF con la API Key (server-to-server)."""
    settings = get_settings()
    _require_api_key(settings)
    url = voucher_pdf_url(voucher_id, lang)
    headers = {
        "Authorization": f"Bearer {settings.interassist_api_key.strip()}",
        "Accept": "application/pdf, application/json",
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        error = InterAssistEmissionError(502, "No se pudo descargar el PDF de Inter Assist")
        error.request_body = {"url": url}
        raise error from exc

    if response.status_code >= 400:
        detail = "Inter Assist rechazó la descarga del PDF"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                detail = str(body["error"])
        except Exception:
            if response.text:
                detail = response.text[:300]
        error = InterAssistEmissionError(
            502 if response.status_code >= 500 else response.status_code,
            detail,
        )
        error.http_status = response.status_code
        error.request_body = {"url": url}
        raise error

    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
        error = InterAssistEmissionError(502, "Inter Assist no devolvió un PDF")
        error.http_status = response.status_code
        error.request_body = {"url": url, "content_type": content_type}
        raise error
    return response.content


def _cliente_dict(cliente: InterAssistClientDraft) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "nombre": cliente.first_name.strip(),
        "apellido": cliente.last_name.strip(),
        "fecha_nacimiento": format_interassist_date(cliente.birth_date),
        "dni": cliente.document_number.strip(),
    }
    if cliente.email:
        payload["email"] = cliente.email.strip()
    if cliente.phone:
        payload["telefono"] = cliente.phone.strip()
    return payload


def _emitir_url(settings: Settings) -> str:
    return f"{settings.interassist_base_url.rstrip('/')}/api/ventas/emitir-externa"


def _payway_cards_url(settings: Settings) -> str:
    return f"{settings.interassist_base_url.rstrip('/')}/api/ventas/checkout/payway/tarjetas"


def _checkout_gateway_url(settings: Settings, gateway: str) -> str:
    return f"{settings.interassist_base_url.rstrip('/')}/api/ventas/checkout/{gateway}"


def _checkout_order_url(settings: Settings, order_uuid: str) -> str:
    return f"{settings.interassist_base_url.rstrip('/')}/api/ventas/checkout/{order_uuid}"


def _venta_url(settings: Settings, venta_id: int) -> str:
    return f"{settings.interassist_base_url.rstrip('/')}/api/ventas/{venta_id}"


def _parse_checkout(
    payload: dict[str, Any],
    status_code: int,
    request_body: dict[str, Any],
    *,
    require_url: bool,
) -> InterAssistCheckoutResult:
    order_uuid = str(payload.get("order_uuid") or "").strip()
    if not order_uuid:
        error = InterAssistEmissionError(502, "Inter Assist no devolvió la orden de checkout")
        error.http_status = status_code
        error.request_body = request_body
        error.response_body = payload
        raise error
    raw_url = payload.get("checkout_url")
    checkout_url = str(raw_url).strip() if raw_url else None
    if require_url and not checkout_url:
        error = InterAssistEmissionError(502, "Inter Assist no devolvió el link de pago")
        error.http_status = status_code
        error.request_body = request_body
        error.response_body = payload
        raise error
    venta = payload.get("venta") if isinstance(payload.get("venta"), dict) else None
    venta_id = _as_int(venta.get("id")) if isinstance(venta, dict) else None
    voucher_ids = _as_int_list(venta.get("voucher_ids")) if isinstance(venta, dict) else []
    gateway = str(payload.get("gateway") or "").strip().lower() or None
    status = str(payload.get("status") or "").strip().lower() or "pending"
    currency = str(payload.get("currency") or "").strip().upper() or None
    return InterAssistCheckoutResult(
        order_uuid=order_uuid,
        status=status,
        checkout_url=checkout_url,
        amount=_as_decimal(payload.get("amount")),
        currency=currency[:3] if currency else None,
        exchange_rate=_as_decimal(payload.get("tipo_cambio")),
        gateway=gateway,
        venta_id=venta_id,
        voucher_ids=voucher_ids,
        status_code=status_code,
        request_body=request_body,
        response_body=payload,
    )


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.interassist_api_key.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request_json(
    client: httpx.Client,
    settings: Settings,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        response = client.request(method, url, headers=_headers(settings), json=json_body)
    except httpx.HTTPError as exc:
        error = InterAssistEmissionError(502, "No se pudo contactar a Inter Assist")
        error.request_body = json_body
        raise error from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": (response.text or "")[:500]}

    if response.status_code >= 400:
        detail = _error_detail(payload, response.status_code)
        error = InterAssistEmissionError(
            422 if response.status_code == 422 else 502,
            detail,
        )
        error.http_status = response.status_code
        error.request_body = json_body
        error.response_body = payload if isinstance(payload, dict) else {"detail": detail}
        raise error

    if not isinstance(payload, dict):
        error = InterAssistEmissionError(502, "Respuesta inválida de Inter Assist")
        error.http_status = response.status_code
        error.request_body = json_body
        raise error
    return payload, response.status_code


def _error_detail(payload: object, status_code: int) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(item) for item in errors[:5])
        if isinstance(errors, dict) and errors:
            parts: list[str] = []
            for key, value in list(errors.items())[:8]:
                if isinstance(value, list):
                    text = "; ".join(str(item) for item in value[:2])
                else:
                    text = str(value)
                parts.append(f"{key}: {text}")
            if parts:
                return "; ".join(parts)
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"Inter Assist respondió {status_code}"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        parsed = _as_int(item)
        if parsed is not None:
            result.append(parsed)
    return result
