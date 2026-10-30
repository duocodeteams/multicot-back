"""Selección de producto Omint: GET de la cotización y AssignProduct.

El precio que se guarda es el que devuelve Omint, no el que manda el panel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings, get_settings
from app.services.companies.omint import (
    _DESTINATION_MAP,
    _HTTP_TIMEOUT,
    _PRODUCT_TYPE_MAP,
    _parse_omint_amount,
    _token_manager,
)

logger = logging.getLogger(__name__)

_DESTINATION_BY_CODE = {code: destination_id for destination_id, code in _DESTINATION_MAP.items()}
_TRIP_BY_PRODUCT_TYPE = {code: trip_type for trip_type, code in _PRODUCT_TYPE_MAP.items()}


# Canales que exigen quotationPaymentMethodFinancingId (guía del 15/09/2026).
FINANCING_REQUIRED_CODES = frozenset({"NPS", "DL", "PWY", "GNET"})

# Pendiente de Omint (mail a DES_OA, sin respuesta todavía):
# - el JSON que envían a urlNotificacion
# - si traen un header para verificar que el aviso es de ellos
# Hasta entonces la URL de notificación lleva el id de la venta y no se interpreta el cuerpo.
# ConfirmPayment es el que dice si el pago está acreditado.

# GUID que pasó Omint para AssignPassangers. Se comparan sin distinguir mayúsculas.
DOCUMENT_TYPE_IDS = {
    "dni": "5C9D2946-99DA-4235-80CD-8375B852390F",
    "passport": "0DDC2670-7355-44A6-A0FE-E36E59618F53",
    "cuit": "ADA0EA3F-DC18-4BFE-B608-D9C4DE98674A",
}


class OmintEmissionError(Exception):
    """Error de lectura o de una llamada que no llegó a modificar pasajeros."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status: int | None = None
        self.request_body: dict[str, Any] | None = None
        self.response_body: dict[str, Any] | None = None


class OmintAssignFailed(OmintEmissionError):
    """AssignProduct se llamó y Omint lo rechazó. La venta queda en emission_failed."""

    def __init__(
        self,
        detail: str,
        selection: OmintSelection,
        http_status: int,
        response_body: dict[str, Any],
    ) -> None:
        super().__init__(502, detail)
        self.selection = selection
        self.http_status = http_status
        self.response_body = response_body


@dataclass
class OmintSelection:
    quotation_id: str
    quotation_number: str | None
    quotation_product_id: str
    product_code: str
    plan_name: str
    currency: str
    amount: Decimal
    base_amount: Decimal | None
    departure_date: date | None
    return_date: date | None
    ages: list[int]
    origin: str | None
    destination_id: int | None
    trip_type: str | None
    days_range: int | None
    provider_context: dict[str, Any] = field(default_factory=dict)
    assign_request: dict[str, Any] = field(default_factory=dict)
    assign_response: dict[str, Any] = field(default_factory=dict)
    assign_status_code: int = 200


def select_omint_product(
    quotation_id: str,
    quotation_product_id: str,
    *,
    client: httpx.Client | None = None,
) -> OmintSelection:
    """Lee la cotización, verifica el producto y lo asigna en Omint."""
    settings = get_settings()
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        quotation, _status = _request_json(
            client,
            settings,
            "GET",
            _quotation_url(settings, quotation_id),
        )
        quotation = _unwrap_quotation(quotation)
        product = _find_product(quotation, quotation_product_id)
        selection = _selection_from_quotation(
            quotation,
            product,
            quotation_id=quotation_id,
            quotation_product_id=quotation_product_id,
        )
        if selection.departure_date is None or selection.return_date is None:
            raise OmintEmissionError(
                502,
                "Omint no informó las fechas de la cotización",
            )
        if selection.amount <= 0:
            raise OmintEmissionError(
                502,
                "Omint no informó el precio del producto",
            )

        request_body = {
            "quotationId": selection.quotation_id,
            "quotationProductId": selection.quotation_product_id,
        }
        try:
            assigned, status_code = _request_json(
                client,
                settings,
                "POST",
                f"{settings.omint_api_base_url.rstrip('/')}/Quotation/AssignProduct",
                json_body=request_body,
            )
        except OmintEmissionError as exc:
            if exc.status_code >= 500 or exc.status_code == 401:
                raise
            raise OmintAssignFailed(
                exc.detail,
                selection,
                exc.status_code,
                {"detail": exc.detail},
            ) from exc

        selected = assigned.get("selectedProduct") if isinstance(assigned, dict) else None
        if isinstance(selected, dict):
            assigned_amount = _amount_from_product(selected)
            if assigned_amount > 0:
                selection.amount = assigned_amount
            assigned_base = _optional_amount(
                selected.get("basePriceAmount")
                if selected.get("basePriceAmount") not in (None, "")
                else selected.get("basePrice")
            )
            if assigned_base is not None and assigned_base > 0:
                selection.base_amount = assigned_base
            if selected.get("productCode"):
                selection.product_code = str(selected["productCode"]).strip() or selection.product_code
            if selected.get("denomination"):
                selection.plan_name = str(selected["denomination"]).strip() or selection.plan_name
            if selected.get("id"):
                selection.quotation_product_id = str(selected["id"])
        selection.assign_request = request_body
        selection.assign_response = assigned if isinstance(assigned, dict) else {"raw": assigned}
        selection.assign_status_code = status_code
        selection.provider_context["quotation_product_id"] = selection.quotation_product_id
        selection.provider_context["product_code"] = selection.product_code
        selection.provider_context["amount"] = str(selection.amount)
        if selection.base_amount is not None:
            selection.provider_context["base_amount"] = str(selection.base_amount)
        return selection
    finally:
        if owns_client:
            client.close()


@dataclass
class OmintPassengerDraft:
    first_name: str
    last_name: str
    birth_date: date
    document_type: str
    document_number: str
    is_holder: bool
    email: str | None = None
    phone: str | None = None
    country_id: str | None = None
    province_id: str | None = None
    street: str | None = None
    street_number: str | None = None
    city: str | None = None


@dataclass
class OmintEmergencyDraft:
    first_name: str
    last_name: str
    phone: str
    email: str | None = None


@dataclass
class OmintPassengersResult:
    request_body: dict[str, Any]
    response_body: dict[str, Any]
    status_code: int
    payment_methods: list[Any]


def assign_omint_passengers(
    quotation_id: str,
    passengers: list[OmintPassengerDraft],
    emergency: OmintEmergencyDraft,
    *,
    receive_promotions: bool = False,
    client: httpx.Client | None = None,
) -> OmintPassengersResult:
    """Carga titular, pasajeros y contacto. No abre el pago."""
    holder = next((passenger for passenger in passengers if passenger.is_holder), None)
    if holder is None:
        raise OmintEmissionError(400, "Tiene que haber un titular")
    request_body = {
        "quotationId": quotation_id,
        "recievePromotionAndNews": receive_promotions,
        "holder": _holder_body(holder),
        "emergencyContact": {
            "name": emergency.first_name,
            "surname": emergency.last_name,
            "phoneNumber": emergency.phone,
            **({"email": emergency.email} if emergency.email else {}),
        },
        "passangers": [_passenger_body(passenger) for passenger in passengers],
    }
    settings = get_settings()
    url = f"{settings.omint_api_base_url.rstrip('/')}/Quotation/AssignPassangers"
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        try:
            status_code, payload, raw = _send(
                client,
                settings,
                "POST",
                url,
                request_body,
                retry_on_401=True,
            )
        except OmintEmissionError as exc:
            exc.request_body = request_body
            raise
    finally:
        if owns_client:
            client.close()
    if status_code == 401:
        error = OmintEmissionError(502, "Omint no autorizó la operación")
        error.http_status = status_code
        error.request_body = request_body
        error.response_body = payload if isinstance(payload, dict) else {"detail": raw}
        raise error
    if status_code >= 400 or not isinstance(payload, dict):
        detail = _error_detail(payload, raw) if status_code >= 400 else "Omint respondió un cuerpo inesperado"
        error = OmintEmissionError(502 if status_code >= 500 or status_code < 400 else status_code, detail)
        error.http_status = status_code
        error.request_body = request_body
        error.response_body = payload if isinstance(payload, dict) else {"detail": detail}
        raise error
    methods = payload.get("paymentMethods")
    return OmintPassengersResult(
        request_body=request_body,
        response_body=payload,
        status_code=status_code,
        payment_methods=methods if isinstance(methods, list) else [],
    )


def list_omint_countries(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Países del mercado del acuerdo, o el listado general si no hay market id."""
    settings = get_settings()
    base = settings.omint_api_base_url.rstrip("/")
    market_id = (settings.omint_market_id or "").strip()
    if market_id:
        url = f"{base}/Country/GetCountriesByMarket/{quote(market_id, safe='')}"
    else:
        url = f"{base}/Country"
    return _catalog(url, client=client)


@dataclass
class OmintCheckout:
    request_body: dict[str, Any]
    response_body: dict[str, Any]
    status_code: int
    payment_url: str


@dataclass
class OmintConfirmation:
    """Resultado de ConfirmPayment. `paid` es false si el pago todavía no está acreditado."""

    paid: bool
    http_status: int
    response_body: dict[str, Any]
    voucher_id: str | None = None
    voucher_number: str | None = None
    voucher_url: str | None = None
    voucher_exists: bool = False


def start_omint_checkout(
    quotation_id: str,
    payment_method_id: str,
    financing_id: str | None,
    *,
    url_ok: str,
    url_error: str,
    url_pending: str,
    url_notification: str,
    client: httpx.Client | None = None,
) -> OmintCheckout:
    """Abre el gateway. Una respuesta 200 significa que el pago se inició, no que se cobró."""
    request_body = {
        "quotationId": quotation_id,
        "quotationPaymentMethodId": payment_method_id,
        "quotationPaymentMethodFinancingId": financing_id,
        "urlOk": url_ok,
        "urlError": url_error,
        "urlPendiente": url_pending,
        "urlNotificacion": url_notification,
    }
    settings = get_settings()
    url = f"{settings.omint_api_base_url.rstrip('/')}/Quotation/ProcessPayment"
    status_code, payload = _post_json(url, request_body, client=client)
    payment_url = ""
    if isinstance(payload, dict):
        payment_url = str(payload.get("paymentUrl") or "").strip()
    if status_code >= 400 or not payment_url:
        detail = (
            _error_detail(payload, "")
            if status_code >= 400
            else "Omint no devolvió la URL de pago"
        )
        error = OmintEmissionError(502 if status_code >= 500 or status_code < 400 else status_code, detail)
        error.http_status = status_code
        error.request_body = request_body
        error.response_body = payload if isinstance(payload, dict) else {"detail": detail}
        raise error
    return OmintCheckout(
        request_body=request_body,
        response_body=payload if isinstance(payload, dict) else {},
        status_code=status_code,
        payment_url=payment_url,
    )


def confirm_omint_checkout(
    quotation_id: str,
    *,
    client: httpx.Client | None = None,
) -> OmintConfirmation:
    """Consulta si el pago está acreditado. Si lo está, Omint crea o recupera el voucher.

    Un 400 con «el pago no ha sido realizado» no es un rechazo definitivo: no hay que
    abrir otro ProcessPayment ni otra cotización. La guía sugiere reconsultar, por
    ejemplo cada 5 segundos durante un minuto; esta función hace una sola consulta.
    """
    settings = get_settings()
    url = f"{settings.omint_api_base_url.rstrip('/')}/Quotation/ConfirmPayment/{quote(quotation_id, safe='')}"
    status_code, payload, raw = _call("GET", url, None, client=client)
    body = payload if isinstance(payload, dict) else {}
    if status_code == 401:
        error = OmintEmissionError(502, "Omint no autorizó la operación")
        error.http_status = status_code
        error.response_body = body or {"detail": raw}
        raise error
    if status_code >= 500:
        error = OmintEmissionError(502, _error_detail(payload, raw))
        error.http_status = status_code
        error.response_body = body or {"detail": raw}
        raise error
    paid = bool(body.get("paymentRecieved")) and status_code < 400
    return OmintConfirmation(
        paid=paid,
        http_status=status_code,
        response_body=body or {"detail": _error_detail(payload, raw)},
        voucher_id=_text(body.get("voucherId")),
        voucher_number=_text(body.get("voucherNumber")),
        voucher_url=_text(body.get("voucherUrl")),
        voucher_exists=bool(body.get("voucherExists")),
    )


def list_omint_provinces(
    country_id: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    settings = get_settings()
    base = settings.omint_api_base_url.rstrip("/")
    url = f"{base}/Province/GetProvincesByCountryAsync/{quote(country_id, safe='')}"
    return _catalog(url, client=client)


def _post_json(
    url: str,
    request_body: dict[str, Any],
    *,
    client: httpx.Client | None,
) -> tuple[int, Any]:
    try:
        status_code, payload, _raw = _call("POST", url, request_body, client=client)
    except OmintEmissionError as exc:
        exc.request_body = request_body
        raise
    return status_code, payload


def _call(
    method: str,
    url: str,
    request_body: dict[str, Any] | None,
    *,
    client: httpx.Client | None,
) -> tuple[int, Any, str]:
    settings = get_settings()
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        return _send(client, settings, method, url, request_body, retry_on_401=True)
    finally:
        if owns_client:
            client.close()


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _holder_body(holder: OmintPassengerDraft) -> dict[str, Any]:
    return {
        "name": holder.first_name,
        "surname": holder.last_name,
        "birthdayDate": holder.birth_date.isoformat(),
        "documentTypeId": _document_type_id(holder.document_type),
        "documentNumber": holder.document_number,
        "phoneNumber": holder.phone or "",
        "email": holder.email or "",
        "countryId": holder.country_id or "",
        "provinceId": holder.province_id or "",
        "street": holder.street or "",
        "streetNumber": holder.street_number or "",
        "city": holder.city or "",
    }


def _passenger_body(passenger: OmintPassengerDraft) -> dict[str, Any]:
    return {
        "name": passenger.first_name,
        "surname": passenger.last_name,
        "birthdayDate": passenger.birth_date.isoformat(),
        "documentTypeId": _document_type_id(passenger.document_type),
        "documentNumber": passenger.document_number,
    }


def _document_type_id(document_type: str) -> str:
    mapped = DOCUMENT_TYPE_IDS.get(document_type.strip().lower())
    if mapped is None:
        raise OmintEmissionError(400, "El tipo de documento no es válido")
    return mapped


def _catalog(url: str, *, client: httpx.Client | None) -> list[dict[str, str]]:
    settings = get_settings()
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        status_code, payload, raw = _send(
            client,
            settings,
            "GET",
            url,
            None,
            retry_on_401=True,
        )
    finally:
        if owns_client:
            client.close()
    if status_code == 401:
        raise OmintEmissionError(502, "Omint no autorizó la operación")
    if status_code >= 400:
        raise OmintEmissionError(
            502 if status_code >= 500 else status_code,
            _error_detail(payload, raw),
        )
    return _catalog_items(payload)


def _catalog_items(payload: Any) -> list[dict[str, str]]:
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("items", "data", "countries", "provinces"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        raise OmintEmissionError(502, "Omint no devolvió el listado")
    items: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get("id")
        name = row.get("name")
        if item_id in (None, "") or name in (None, ""):
            continue
        items.append({"id": str(item_id), "name": str(name)})
    return items


def _quotation_url(settings: Settings, quotation_id: str) -> str:
    base = settings.omint_api_base_url.rstrip("/")
    return f"{base}/Quotation/{quote(quotation_id, safe='')}"


def _request_json(
    client: httpx.Client,
    settings: Settings,
    method: str,
    url: str,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    status_code, payload, raw = _send(client, settings, method, url, json_body, retry_on_401=True)
    if status_code == 404:
        raise OmintEmissionError(404, "No encontramos esa cotización de Omint")
    if status_code == 401:
        raise OmintEmissionError(502, "Omint no autorizó la operación")
    if status_code >= 400:
        raise OmintEmissionError(status_code, _error_detail(payload, raw))
    if not isinstance(payload, dict):
        raise OmintEmissionError(502, "Omint respondió un cuerpo inesperado")
    return payload, status_code


def _send(
    client: httpx.Client,
    settings: Settings,
    method: str,
    url: str,
    json_body: dict[str, Any] | None,
    *,
    retry_on_401: bool,
) -> tuple[int, Any, str]:
    token = _token_manager.get_token(client, settings)
    try:
        response = client.request(
            method,
            url,
            json=json_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        logger.exception("Omint: error de red en %s %s", method, url)
        raise OmintEmissionError(502, "Omint no respondió") from exc

    if response.status_code == 401 and retry_on_401:
        logger.warning("Omint: 401 en %s; renovando token y reintentando", method)
        _token_manager.force_refresh(client, settings)
        return _send(client, settings, method, url, json_body, retry_on_401=False)

    raw = response.text[:800]
    payload: Any = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    if response.status_code >= 400:
        logger.error(
            "Omint: %s %s HTTP %s body=%s",
            method,
            url,
            response.status_code,
            raw,
        )
    return response.status_code, payload, raw


def _unwrap_quotation(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("id") or payload.get("products") or payload.get("dateSince"):
        return payload
    nested = payload.get("quotation")
    if isinstance(nested, dict):
        return nested
    return payload


def _find_product(quotation: dict[str, Any], quotation_product_id: str) -> dict[str, Any] | None:
    products = quotation.get("products")
    if not isinstance(products, list) or not products:
        return None
    for product in products:
        if isinstance(product, dict) and _same_id(product.get("id"), quotation_product_id):
            return product
    raise OmintEmissionError(400, "Ese producto no pertenece a la cotización de Omint")


def _selection_from_quotation(
    quotation: dict[str, Any],
    product: dict[str, Any] | None,
    *,
    quotation_id: str,
    quotation_product_id: str,
) -> OmintSelection:
    product = product or {}
    product_code = str(product.get("productCode") or quotation_product_id).strip()
    plan_name = str(product.get("denomination") or product_code).strip()
    amount = _amount_from_product(product) if product else Decimal("0")
    base_amount = None
    if product:
        base_amount = _optional_amount(
            product.get("basePriceAmount")
            if product.get("basePriceAmount") not in (None, "")
            else product.get("basePrice")
        )
        if base_amount is not None and base_amount <= 0:
            base_amount = None
    currency = str(product.get("currencyCode") or "ARS").strip() or "ARS"
    resolved_quotation_id = str(quotation.get("id") or quotation_id)
    number = quotation.get("number")
    quotation_number = str(number).strip() if number not in (None, "") else None
    destination_code = _first_text(
        quotation.get("destinationCode"),
        product.get("destinationCode"),
    )
    product_type_code = _first_text(
        quotation.get("productTypeCode"),
        product.get("productTypeCode"),
    )
    currency_rate = _optional_amount(product.get("currencyRate"))
    original_final = _optional_amount(product.get("originalFinalPriceAmount"))
    original_currency = _first_text(product.get("originalCurrencyCode"))
    context: dict[str, Any] = {
        "quotation_id": resolved_quotation_id,
        "quotation_product_id": str(product.get("id") or quotation_product_id),
        "quotation_number": quotation_number,
        "product_code": product_code,
        "currency": currency,
        "amount": str(amount),
    }
    if destination_code:
        context["destination_code"] = destination_code
    if product_type_code:
        context["product_type_code"] = product_type_code
    if currency_rate is not None and currency_rate > 0:
        context["currency_rate"] = str(currency_rate)
    if original_final is not None and original_final > 0:
        context["original_final_price_amount"] = str(original_final)
    if original_currency:
        context["original_currency_code"] = original_currency
    if base_amount is not None:
        context["base_amount"] = str(base_amount)

    return OmintSelection(
        quotation_id=resolved_quotation_id,
        quotation_number=quotation_number,
        quotation_product_id=str(product.get("id") or quotation_product_id),
        product_code=product_code,
        plan_name=plan_name,
        currency=currency[:3],
        amount=amount,
        base_amount=base_amount,
        departure_date=_parse_date(quotation.get("dateSince")),
        return_date=_parse_date(quotation.get("dateUntil")),
        ages=_ages(quotation.get("ages")),
        origin=_origin(quotation),
        destination_id=_DESTINATION_BY_CODE.get(destination_code or ""),
        trip_type=_TRIP_BY_PRODUCT_TYPE.get(product_type_code or ""),
        days_range=_days_range(quotation.get("quantityOfDays")),
        provider_context=context,
    )


def _amount_from_product(product: dict[str, Any]) -> Decimal:
    raw = product.get("finalPriceAmount")
    if raw in (None, ""):
        raw = product.get("finalPrice")
    return _parse_omint_amount(raw)


def _optional_amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    amount = _parse_omint_amount(value)
    return amount


def _ages(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    ages: list[int] = []
    for item in value:
        try:
            ages.append(int(item))
        except (TypeError, ValueError):
            continue
    return ages


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text_value = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text_value).date()
    except ValueError:
        return None


def _origin(quotation: dict[str, Any]) -> str | None:
    code = _first_text(quotation.get("departureCode"))
    if code:
        return code[:16]
    return None


def _days_range(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _same_id(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return str(left).strip().lower() == str(right).strip().lower()


def _error_detail(payload: Any, raw: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            message = str(errors[0]).strip()
            if message:
                return message[:300]
        title = payload.get("title") or payload.get("detail") or payload.get("message")
        if isinstance(title, str) and title.strip():
            return title.strip()[:300]
    if raw.strip():
        return "Omint rechazó la operación"
    return "Omint rechazó la operación"
