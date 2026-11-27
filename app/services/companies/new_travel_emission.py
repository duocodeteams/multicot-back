"""Emisión y plataforma de pagos New Travel (orders/create + paymentLink)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.companies.new_travel import DESTINATION_TO_TERRITORY

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0

# Catálogo para el panel: mismos ids que PassengersCreate (dni/passport/cuit).
# Al emitir se mapean a códigos New Travel (PAS/CC/CE) en map_document_type.
DOCUMENT_TYPES = (
    {"id": "passport", "name": "Pasaporte"},
    {"id": "dni", "name": "Cédula / DNI"},
    {"id": "cuit", "name": "Cédula de extranjería"},
)

# Nuestro form (dni/passport/cuit) → códigos New Travel.
DOCUMENT_TYPE_MAP = {
    "passport": "PAS",
    "pas": "PAS",
    "dni": "CC",
    "cc": "CC",
    "cuit": "CE",
    "ce": "CE",
}


class NewTravelEmissionError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        http_status: int | None = None,
        request_body: dict[str, Any] | None = None,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status = http_status
        self.request_body = request_body
        self.response_body = response_body


@dataclass
class NewTravelPassengerDraft:
    first_name: str
    last_name: str
    birth_date: date
    document_type: str
    document_number: str
    email: str
    phone: str
    country: str
    code_phone: str = "54"
    sex: str | None = None
    medical_conditions: str = ""


@dataclass
class NewTravelContactDraft:
    name: str
    email: str
    phone: str
    code_phone: str = "54"


@dataclass
class NewTravelCreateResult:
    voucher_codes: list[str]
    order_status: int | None
    wait_for_payment: str | None
    voucher_url: str | None
    total: str | None
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200


@dataclass
class NewTravelPaymentLinkResult:
    code: str
    order_status: int | None
    total: str | None
    url: str
    status: str | None
    created_at: str | None
    expires_at: str | None
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200


@dataclass
class NewTravelOrderStatus:
    code: str
    paid: bool
    order_status: str | None
    voucher_url: str | None
    payment_link_status: str | None
    payment_link_url: str | None
    response_body: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200


def list_new_travel_document_types() -> list[dict[str, str]]:
    return [dict(item) for item in DOCUMENT_TYPES]


def build_voucher_print_url(code: str) -> str | None:
    """URL pública del PDF. GET /orders/order no la devuelve; el print es /print/{code}."""
    safe = (code or "").strip()
    if not safe:
        return None
    base = get_settings().new_travel_base_url.rstrip("/")
    root = re.sub(r"/api/v\d+$", "", base).rstrip("/") or base
    return f"{root}/print/{safe}"


def list_new_travel_countries(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Países New Travel: id = ISO (ej. AR)."""
    payload = _api_get("/generals/countries", client=client)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise NewTravelEmissionError(502, "New Travel no devolvió países")
    items: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        iso = str(row.get("isoCountry") or "").strip().upper()
        name = str(row.get("description") or "").strip()
        if not iso or not name:
            continue
        items.append({"id": iso, "name": name})
    return items


def territory_for_destination(destination_id: int | None) -> str:
    if destination_id is None:
        raise NewTravelEmissionError(400, "La venta no tiene destino")
    territory = DESTINATION_TO_TERRITORY.get(destination_id)
    if not territory:
        raise NewTravelEmissionError(400, "Destino sin mapeo de territorio New Travel")
    return territory


def map_document_type(document_type: str) -> str:
    mapped = DOCUMENT_TYPE_MAP.get(document_type.strip().lower())
    if mapped is None:
        raise NewTravelEmissionError(
            400,
            "Tipo de documento no soportado para New Travel (usá passport, dni o cuit)",
        )
    return mapped


def create_order_pending_payment(
    *,
    plan_id: str,
    origin: str,
    destination_territory: str,
    start_date: date,
    end_date: date,
    contact: NewTravelContactDraft,
    beneficiaries: list[NewTravelPassengerDraft],
    days_range: int | None = None,
    individual_voucher: bool = True,
    reference: str = "",
    comment: str = "",
    client: httpx.Client | None = None,
) -> NewTravelCreateResult:
    if not beneficiaries:
        raise NewTravelEmissionError(400, "Hace falta al menos un beneficiario")
    body: dict[str, Any] = {
        "idPlan": str(plan_id),
        "origin": origin.strip().upper()[:2],
        "destination": str(destination_territory),
        "startDate": _fmt_date(start_date),
        "endDate": _fmt_date(end_date),
        "nameContact": contact.name,
        "emailContact": contact.email,
        "codePhoneContact": contact.code_phone,
        "phoneContact": _digits_phone(contact.phone),
        "reference": reference,
        "individualVoucher": individual_voucher,
        "comment": comment,
        "waitForPayment": "Y",
        "sendMail": "Y",
        "beneficiaries": [_beneficiary_body(item) for item in beneficiaries],
    }
    if days_range is not None:
        body["daysRange"] = str(days_range)

    status_code, payload = _api_json("POST", "/orders/create", body, client=client)
    if _payload_failed(payload):
        raise _api_error(
            status_code if status_code >= 400 else 400,
            payload,
            "New Travel rechazó la emisión",
            request_body=body,
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if status_code >= 400 or not isinstance(data, dict):
        raise _api_error(
            status_code,
            payload,
            "New Travel rechazó la emisión",
            request_body=body,
        )

    codes_raw = data.get("voucherCode") or data.get("voucherCodes") or []
    if isinstance(codes_raw, str):
        codes = [codes_raw.strip()] if codes_raw.strip() else []
    elif isinstance(codes_raw, list):
        codes = [str(c).strip() for c in codes_raw if str(c).strip()]
    else:
        codes = []
    if not codes:
        raise NewTravelEmissionError(
            502,
            "New Travel no devolvió el código de voucher",
            http_status=status_code,
            request_body=body,
            response_body=payload if isinstance(payload, dict) else {"detail": payload},
        )

    voucher_url = _as_text(data.get("voucher"))
    if not voucher_url and codes:
        # Sin waitForPayment a veces viene data.voucher; si falta, armar print URL.
        wait = (_as_text(data.get("waitForPayment")) or "").upper()
        if wait != "Y":
            voucher_url = build_voucher_print_url(codes[0])

    return NewTravelCreateResult(
        voucher_codes=codes,
        order_status=_as_int(data.get("orderStatus")),
        wait_for_payment=_as_text(data.get("waitForPayment")),
        voucher_url=voucher_url,
        total=_as_text(data.get("total")),
        request_body=body,
        response_body=payload if isinstance(payload, dict) else {},
        http_status=status_code,
    )


def create_payment_link(
    code: str,
    *,
    email: str | None = None,
    client: httpx.Client | None = None,
) -> NewTravelPaymentLinkResult:
    body: dict[str, Any] = {"code": code.strip()}
    if email and email.strip():
        body["email"] = email.strip()
    status_code, payload = _api_json("POST", "/payments/paymentLink", body, client=client)
    if _payload_failed(payload):
        raise _api_error(
            status_code if status_code >= 400 else 400,
            payload,
            "New Travel no devolvió el link de pago",
            request_body=body,
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    link = data.get("paymentLink") if isinstance(data, dict) else None
    url = ""
    if isinstance(link, dict):
        url = str(link.get("url") or "").strip()
    if status_code >= 400 or not isinstance(data, dict) or not url:
        raise _api_error(
            status_code,
            payload,
            "New Travel no devolvió el link de pago",
            request_body=body,
        )
    return NewTravelPaymentLinkResult(
        code=str(data.get("code") or code).strip(),
        order_status=_as_int(data.get("orderStatus")),
        total=_as_text(data.get("total")),
        url=url,
        status=_as_text(link.get("status")) if isinstance(link, dict) else None,
        created_at=_as_text(link.get("createdAt")) if isinstance(link, dict) else None,
        expires_at=_as_text(link.get("expiresAt")) if isinstance(link, dict) else None,
        request_body=body,
        response_body=payload if isinstance(payload, dict) else {},
        http_status=status_code,
    )


def get_order(code: str, *, client: httpx.Client | None = None) -> NewTravelOrderStatus:
    safe = code.strip()
    status_code, payload = _api_json(
        "GET",
        f"/orders/order/{safe}",
        None,
        client=client,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if status_code >= 400 or not isinstance(data, dict):
        raise _api_error(
            status_code,
            payload,
            "New Travel no devolvió la emisión",
            request_body={"code": safe},
        )

    paid_raw = data.get("paid")
    paid = str(paid_raw).strip() in {"1", "true", "True", "Y", "y"}
    link = data.get("paymentLink")
    link_status = None
    link_url = None
    if isinstance(link, dict):
        link_status = _as_text(link.get("status"))
        link_url = _as_text(link.get("url"))
        if (link_status or "").upper() == "USED":
            paid = True

    order_status = _as_text(data.get("status")) or _as_text(data.get("orderStatus"))
    if order_status == "1":
        # Activa suele implicar cobrada en este flujo con waitForPayment.
        paid = True

    order_code = str(data.get("code") or safe).strip()
    voucher_url = _as_text(
        data.get("voucher")
        or data.get("voucherUrl")
        or data.get("printUrl")
        or data.get("link")
    )
    # Consultar emisión no incluye la URL de impresión; con pago confirmado
    # el PDF público es {host}/print/{code} (mismo patrón que data.voucher al crear).
    if not voucher_url and paid:
        voucher_url = build_voucher_print_url(order_code)

    return NewTravelOrderStatus(
        code=order_code,
        paid=paid,
        order_status=order_status,
        voucher_url=voucher_url,
        payment_link_status=link_status,
        payment_link_url=link_url,
        response_body=payload if isinstance(payload, dict) else {},
        http_status=status_code,
    )


def send_voucher(
    code: str,
    email: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    body = {"code": code.strip(), "email": email.strip()}
    status_code, payload = _api_json("POST", "/orders/sendVoucher", body, client=client)
    if status_code >= 400:
        raise _api_error(
            status_code,
            payload,
            "New Travel no pudo enviar el voucher",
            request_body=body,
        )
    return payload if isinstance(payload, dict) else {"status": "OK"}


def _beneficiary_body(passenger: NewTravelPassengerDraft) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": passenger.first_name,
        "lastName": passenger.last_name,
        "birthdate": _fmt_date(passenger.birth_date),
        "country": passenger.country.strip().upper()[:2],
        "typeDocument": map_document_type(passenger.document_type),
        "document": passenger.document_number,
        "email": passenger.email,
        "codePhone": passenger.code_phone,
        "phone": _digits_phone(passenger.phone),
        "medicalConditions": passenger.medical_conditions or "",
    }
    if passenger.sex:
        body["sex"] = passenger.sex.strip().lower()[:1]
    return body


def _fmt_date(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def _digits_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits or (value or "").strip()


def _as_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _api_error(
    status_code: int,
    payload: Any,
    fallback: str,
    *,
    request_body: dict[str, Any] | None = None,
) -> NewTravelEmissionError:
    detail = _error_detail(payload) or fallback
    mapped = 502 if status_code >= 500 or status_code < 400 else status_code
    return NewTravelEmissionError(
        mapped,
        detail,
        http_status=status_code,
        request_body=request_body,
        response_body=payload if isinstance(payload, dict) else {"detail": detail},
    )


def _payload_failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().upper()
    return status in {"ERROR", "UNAUTHORIZED", "NOT_FOUND"}


def _error_detail(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("description")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
        code = err.get("code")
        if code not in (None, ""):
            return f"New Travel error {code}"[:300]
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:300]
    return None


def _api_get(path: str, *, client: httpx.Client | None) -> dict[str, Any]:
    status_code, payload = _api_json("GET", path, None, client=client)
    if status_code >= 400 or not isinstance(payload, dict):
        raise _api_error(status_code, payload, "New Travel rechazó la consulta")
    return payload


def _api_json(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    client: httpx.Client | None,
) -> tuple[int, Any]:
    settings = get_settings()
    if not settings.new_travel_user or not settings.new_travel_password:
        raise NewTravelEmissionError(500, "Faltan NEW_TRAVEL_USER/NEW_TRAVEL_PASSWORD")
    base = settings.new_travel_base_url.rstrip("/")
    owns = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        token = _get_token(client, base)
        return _send(client, base, token, method, path, body, retry_on_auth_error=True)
    finally:
        if owns:
            client.close()


def _get_token(client: httpx.Client, base_url: str) -> str:
    settings = get_settings()
    try:
        response = client.post(
            f"{base_url}/auth/get_token",
            json={
                "user": settings.new_travel_user,
                "password": settings.new_travel_password,
            },
            headers={"Language": "spa", "Content-Type": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.exception("NewTravel emission: error de red en get_token")
        raise NewTravelEmissionError(502, "New Travel no respondió") from exc
    raw = response.text[:800]
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        raise NewTravelEmissionError(
            502,
            _error_detail(payload) or "New Travel no autorizó la operación",
            http_status=response.status_code,
            response_body=payload if isinstance(payload, dict) else {"detail": raw},
        )
    token = (payload.get("data") or {}).get("token") if isinstance(payload, dict) else None
    if not token:
        raise NewTravelEmissionError(502, "New Travel no devolvió token")
    return str(token)


def _send(
    client: httpx.Client,
    base_url: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    retry_on_auth_error: bool,
) -> tuple[int, Any]:
    url = f"{base_url}{path}"
    # Un solo header: Token + token juntos provoca 401 (credenciales no válidas).
    headers = {
        "token": token,
        "Language": "spa",
        "Content-Type": "application/json",
    }
    try:
        response = client.request(method, url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.exception("NewTravel emission: error de red en %s %s", method, path)
        raise NewTravelEmissionError(502, "New Travel no respondió") from exc

    raw = response.text[:800]
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = None

    # Token vencido / IP: renovar una vez.
    err_code = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err_code = str(payload["error"].get("code") or "")
    if retry_on_auth_error and (
        response.status_code in {401, 403} or err_code in {"1002", "1003"}
    ):
        logger.warning("NewTravel emission: auth error en %s; renovando token", path)
        new_token = _get_token(client, base_url)
        return _send(
            client,
            base_url,
            new_token,
            method,
            path,
            body,
            retry_on_auth_error=False,
        )

    if response.status_code >= 400:
        logger.error(
            "NewTravel emission: %s %s HTTP %s body=%s",
            method,
            path,
            response.status_code,
            raw,
        )
    return response.status_code, payload
