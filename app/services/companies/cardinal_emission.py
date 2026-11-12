"""Emisión Cardinal Assistance vía POST /webservice/emitir.

Cardinal carga el importe en la cuenta corriente del agente. El cobro al
pasajero lo hace nuestra pasarela. montoCotizado es el amount de costoFinal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_cardinal_destino_ids, get_settings
from app.credentials.schemas import CardinalCredentials
from app.quotations.schemas import TRIP_TYPE_MULTIVIAJE, TRIP_TYPE_UNICO_VIAJE

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_GUID = re.compile(r"^[a-z0-9]{32}$")
_SECRET = re.compile(r"^[a-z0-9]{96}$")
# Ids vistos en /parametros parametros=monedas. El resto queda como XXX.
_CURRENCY_CODES = {3: "USD", 7: "ARS"}
_DOCUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "dni": ("dni", "documento nacional de identidad", "documento"),
    "passport": ("pasaporte", "passport"),
    "cuit": ("cuit", "cuil", "cuit/cuil"),
}


class CardinalEmissionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status: int | None = None
        self.request_body: dict[str, Any] | None = None
        self.response_body: dict[str, Any] | None = None


@dataclass
class CardinalQuoteSelection:
    cotizacion_guid: str
    producto_id: int
    plan_name: str
    amount: Decimal
    currency_id: int
    currency: str
    base_amount: Decimal | None
    origen_id: int | None
    destino_id: int | None
    destination_id: int | None
    departure_date: date
    return_date: date | None
    ages: list[int]
    trip_type: str
    days_range: int | None
    request_body: dict[str, Any]
    response_body: dict[str, Any]


@dataclass
class CardinalPassengerDraft:
    first_name: str
    last_name: str
    birth_date: date
    document_type: str
    document_type_id: int
    document_number: str
    email: str
    phone: str
    address: str
    city: str
    country_id: int
    emergency_first_name: str
    emergency_last_name: str
    emergency_phone: str


@dataclass
class CardinalVoucher:
    number: str
    url: str | None
    document_number: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CardinalEmissionResult:
    vouchers: list[CardinalVoucher]
    status_code: int
    request_body: dict[str, Any]
    response_body: dict[str, Any]


def fetch_cardinal_quotation(
    credentials: CardinalCredentials,
    cotizacion_guid: str,
    producto_id: int,
    *,
    client: httpx.Client | None = None,
) -> CardinalQuoteSelection:
    """Trae la cotización y congela costoFinal del producto elegido."""
    _require_credentials(credentials)
    guid = cotizacion_guid.strip().lower()
    if not _GUID.fullmatch(guid):
        raise CardinalEmissionError(400, "El id de la cotización de Cardinal no es válido")
    body = {
        "agenteEmisorGuid": credentials.agente_emisor_guid.strip().lower(),
        "agenteEmisorSecreto": credentials.agente_emisor_secreto.strip().lower(),
        "cotizacionGuid": guid,
    }
    payload, status_code = _post(client, "/obtener_cotizacion", body)
    cotizacion = payload.get("cotizacion")
    if not isinstance(cotizacion, dict):
        error = CardinalEmissionError(502, "Cardinal no devolvió la cotización")
        error.http_status = status_code
        error.request_body = redact_cardinal_body(body)
        error.response_body = payload
        raise error
    product = _find_product(cotizacion.get("productos"), producto_id)
    if product is None:
        raise CardinalEmissionError(400, "Ese producto no está en la cotización de Cardinal")
    money = _money(product.get("costoFinal"))
    if money is None:
        raise CardinalEmissionError(502, "Cardinal no informó costoFinal para ese producto")
    amount, currency_id = money
    base = _money(product.get("costoLista"))
    params = cotizacion.get("parametros") if isinstance(cotizacion.get("parametros"), dict) else {}
    departure = _parse_date(params.get("fechaSalida"))
    if departure is None:
        raise CardinalEmissionError(502, "Cardinal no informó la fecha de salida")
    retorno = _parse_date(params.get("fechaRegreso"))
    ages = _ages(params.get("edades"))
    if not ages:
        raise CardinalEmissionError(502, "Cardinal no informó las edades de la cotización")
    annual = bool(params.get("tarifaAnual"))
    days_range = _as_int(params.get("maxDiasCorridos")) if annual else None
    origen_id = _as_int(params.get("origenId"))
    destino_id = _as_int(params.get("destinoId"))
    plan_name = str(product.get("productoNombre") or "").strip() or str(producto_id)
    return CardinalQuoteSelection(
        cotizacion_guid=guid,
        producto_id=producto_id,
        plan_name=plan_name[:200],
        amount=amount,
        currency_id=currency_id,
        currency=_CURRENCY_CODES.get(currency_id, "XXX"),
        base_amount=base[0] if base else None,
        origen_id=origen_id,
        destino_id=destino_id,
        destination_id=_our_destination_id(destino_id),
        departure_date=departure,
        return_date=retorno,
        ages=ages,
        trip_type=TRIP_TYPE_MULTIVIAJE if annual else TRIP_TYPE_UNICO_VIAJE,
        days_range=days_range,
        request_body=redact_cardinal_body(body),
        response_body=payload,
    )


def list_cardinal_parameters(
    credentials: CardinalCredentials,
    parametros: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    """tiposDocumentos o paisesResidencia. Cada ítem es id y nombre."""
    guid = credentials.agente_emisor_guid.strip().lower()
    if not _GUID.fullmatch(guid):
        raise CardinalEmissionError(400, "Falta el agente emisor de Cardinal")
    body = {"agenteEmisorGuid": guid, "parametros": parametros, "localeId": 1}
    payload, _status = _post(client, "/parametros", body)
    raw = payload.get(parametros)
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        items.append({"id": str(row["id"]), "name": str(row.get("nombre") or "").strip()})
    return items


def document_type_id(document_type: str, catalog: list[dict[str, str]]) -> int:
    aliases = _DOCUMENT_ALIASES.get(document_type.strip().lower())
    if not aliases:
        raise CardinalEmissionError(400, "Tipo de documento no soportado para Cardinal")
    for item in catalog:
        name = _fold(item.get("name") or "")
        if name in aliases or any(alias in name for alias in aliases):
            parsed = _as_int(item.get("id"))
            if parsed is not None:
                return parsed
    raise CardinalEmissionError(
        400,
        "Cardinal no tiene un tipo de documento para " + document_type,
    )


def build_emitir_payload(
    credentials: CardinalCredentials,
    cotizacion_guid: str,
    producto_id: int,
    amount: Decimal,
    passengers: list[CardinalPassengerDraft],
) -> dict[str, Any]:
    _require_credentials(credentials)
    if not passengers or len(passengers) > 50:
        raise CardinalEmissionError(400, "Cardinal acepta entre 1 y 50 pasajeros")
    return {
        "agenteEmisorGuid": credentials.agente_emisor_guid.strip().lower(),
        "agenteEmisorSecreto": credentials.agente_emisor_secreto.strip().lower(),
        "cotizacionGuid": cotizacion_guid.strip().lower(),
        "productoSeleccionadoId": producto_id,
        "montoCotizado": float(amount.quantize(Decimal("0.01"))),
        "pasajeros": [_passenger_dict(passenger) for passenger in passengers],
    }


def emit_cardinal_sale(
    credentials: CardinalCredentials,
    cotizacion_guid: str,
    producto_id: int,
    amount: Decimal,
    passengers: list[CardinalPassengerDraft],
    *,
    client: httpx.Client | None = None,
) -> CardinalEmissionResult:
    body = build_emitir_payload(
        credentials, cotizacion_guid, producto_id, amount, passengers
    )
    payload, status_code = _post(client, "/emitir", body)
    raw_vouchers = payload.get("vouchers")
    if not isinstance(raw_vouchers, list) or not raw_vouchers:
        error = CardinalEmissionError(502, "Cardinal no devolvió los vouchers")
        error.http_status = status_code
        error.request_body = redact_cardinal_body(body)
        error.response_body = payload
        raise error
    vouchers: list[CardinalVoucher] = []
    for row in raw_vouchers:
        if not isinstance(row, dict):
            continue
        number = str(row.get("nroVoucher") or "").strip()
        if not number:
            continue
        link = str(row.get("linkImprimir") or "").strip() or None
        document = str(row.get("nroDocumento") or "").strip() or None
        vouchers.append(
            CardinalVoucher(number=number, url=link, document_number=document, raw=row)
        )
    if len(vouchers) != len(passengers):
        error = CardinalEmissionError(502, "Cardinal no devolvió un voucher por pasajero")
        error.http_status = status_code
        error.request_body = redact_cardinal_body(body)
        error.response_body = payload
        raise error
    return CardinalEmissionResult(
        vouchers=vouchers,
        status_code=status_code,
        request_body=redact_cardinal_body(body),
        response_body=payload,
    )


def redact_cardinal_body(body: dict[str, Any]) -> dict[str, Any]:
    safe = dict(body)
    if "agenteEmisorSecreto" in safe:
        safe["agenteEmisorSecreto"] = "***"
    return safe


def _require_credentials(credentials: CardinalCredentials) -> None:
    guid = credentials.agente_emisor_guid.strip().lower()
    secret = credentials.agente_emisor_secreto.strip().lower()
    if not _GUID.fullmatch(guid):
        raise CardinalEmissionError(400, "Falta el agente emisor de Cardinal")
    if not secret:
        raise CardinalEmissionError(
            400,
            "Falta el secreto del agente emisor de Cardinal para emitir",
        )
    if not _SECRET.fullmatch(secret):
        raise CardinalEmissionError(400, "El secreto del agente emisor de Cardinal no es válido")


def _passenger_dict(passenger: CardinalPassengerDraft) -> dict[str, Any]:
    return {
        "apellido": passenger.last_name.strip()[:100],
        "nombre": passenger.first_name.strip()[:100],
        "email": passenger.email.strip()[:100],
        "tipoDocumentoId": passenger.document_type_id,
        "nroDocumento": passenger.document_number.strip()[:20],
        "fechaNacimiento": passenger.birth_date.isoformat(),
        "telefono": passenger.phone.strip()[:50],
        "domicilio": passenger.address.strip()[:250],
        "localidad": passenger.city.strip()[:100],
        "paisId": passenger.country_id,
        "emergenciaNombre": passenger.emergency_first_name.strip()[:100],
        "emergenciaApellido": passenger.emergency_last_name.strip()[:100],
        "emergenciaTelefono": passenger.emergency_phone.strip()[:50],
    }


def _find_product(productos: object, producto_id: int) -> dict[str, Any] | None:
    if not isinstance(productos, list):
        return None
    for product in productos:
        if isinstance(product, dict) and _as_int(product.get("productoId")) == producto_id:
            return product
    return None


def _money(value: object) -> tuple[Decimal, int] | None:
    if not isinstance(value, dict) or value.get("amount") is None:
        return None
    try:
        amount = Decimal(str(value["amount"])).quantize(Decimal("0.01"))
    except Exception:
        return None
    if amount <= 0:
        return None
    return amount, _as_int(value.get("currency")) or 0


def _ages(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    ages: list[int] = []
    for item in value:
        parsed = _as_int(item)
        if parsed is not None:
            ages.append(parsed)
    return ages


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _our_destination_id(destino_id: int | None) -> int | None:
    if destino_id is None:
        return None
    for ours, theirs in get_cardinal_destino_ids(get_settings()).items():
        if theirs == destino_id:
            return ours
    return None


def _fold(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )


def _post(
    client: httpx.Client | None,
    path: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    settings = get_settings()
    url = f"{settings.cardinal_base_url.rstrip('/')}{path}"
    owned = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        response = http.post(url, json=body)
    except httpx.HTTPError as exc:
        error = CardinalEmissionError(502, "No se pudo contactar a Cardinal")
        error.request_body = redact_cardinal_body(body)
        raise error from exc
    finally:
        if owned:
            http.close()

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": (response.text or "")[:500]}
    if not isinstance(payload, dict):
        payload = {"raw": str(payload)[:500]}

    if response.status_code >= 400 or payload.get("resultado") == "error":
        detail = _error_detail(payload, response.status_code)
        status = 400 if response.status_code == 400 or payload.get("errorCode") == 4 else 502
        error = CardinalEmissionError(status, detail)
        error.http_status = response.status_code
        error.request_body = redact_cardinal_body(body)
        error.response_body = payload
        raise error
    return payload, response.status_code


def _error_detail(payload: dict[str, Any], status_code: int) -> str:
    messages = payload.get("mensajes")
    if isinstance(messages, list):
        text = "; ".join(str(item).strip() for item in messages if str(item).strip())
        if text:
            return text[:500]
    return f"Cardinal respondió {status_code}"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None
