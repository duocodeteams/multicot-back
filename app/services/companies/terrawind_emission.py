"""Emisión Terrawind (SETW): check_voucher, add_vouchers y URL de PDF.

El cobro lo hace Nave. Este módulo solo valida y emite vouchers vía POST form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 60.0

DOCUMENT_TYPE_IDS = {
    "dni": "1",
    "passport": "2",
    "cuit": "3",
}


class TerrawindEmissionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status: int | None = None
        self.request_body: dict[str, Any] | None = None
        self.response_body: dict[str, Any] | None = None


@dataclass
class TerrawindPassengerDraft:
    document_type: str
    document_number: str
    birth_date: date
    gender: str
    first_name: str
    last_name: str
    second_name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    address: str | None = None
    residence_country_id: str | None = None
    emergency_first_name: str = ""
    emergency_last_name: str = ""
    emergency_phone_1: str = ""
    emergency_phone_2: str | None = None


@dataclass
class TerrawindIssuedVoucher:
    voucher_number: str
    voucher_key: str
    passenger_document_number: str | None = None
    voucher_int_ref: str | None = None
    pdf_url: str | None = None


@dataclass
class TerrawindEmissionResult:
    vouchers: list[TerrawindIssuedVoucher]
    status_code: int
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)


def format_setw_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def document_type_id(document_type: str) -> str:
    key = document_type.strip().lower()
    mapped = DOCUMENT_TYPE_IDS.get(key)
    if mapped is None:
        raise TerrawindEmissionError(400, f"Tipo de documento no soportado: {document_type}")
    return mapped


def normalize_gender(raw: str) -> str:
    value = raw.strip().lower()
    if value not in {"m", "f"}:
        raise TerrawindEmissionError(400, "El género del pasajero debe ser m o f")
    return value


def voucher_pdf_url(
    voucher_number: str,
    voucher_key: str,
    *,
    lang: str = "es",
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    base = settings.terrawind_base_url.strip().lower()
    if "sandbox" in base:
        host = "https://sandbox.setw.net/emision/voucher.php"
    else:
        host = "https://emision.setw.net/voucher.php"
    lang_q = lang.strip().lower() or "es"
    return (
        f"{host}?voucher_number={voucher_number}"
        f"&voucher_key={voucher_key}&change_lang={lang_q}"
    )


def list_setw_countries(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    root = _call_action("get_countries", {}, client=client)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for node in root.findall("country"):
        fields = _xml_child_map(node)
        country_id = fields.get("country_id", "").strip()
        name = fields.get("country_name", "").strip()
        if country_id and name:
            items.append({"id": country_id, "name": name})
    return items


def list_setw_document_types(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    root = _call_action("get_document_types", {}, client=client)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for node in root.findall("document_type"):
        fields = _xml_child_map(node)
        doc_id = fields.get("document_type_id", "").strip()
        name = fields.get("document_type_name", "").strip()
        if doc_id and name:
            items.append({"id": doc_id, "name": name})
    return items


def check_voucher(
    *,
    product_id: str,
    country_id: str,
    departure_date: date,
    return_date: date,
    passenger: TerrawindPassengerDraft,
    voucher_int_ref: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Valida datos antes del cobro. OK → sin error; si falla levanta TerrawindEmissionError."""
    payload = _passenger_payload(
        product_id=product_id,
        country_id=country_id,
        departure_date=departure_date,
        return_date=return_date,
        passenger=passenger,
        voucher_int_ref=voucher_int_ref,
        index=None,
    )
    root = _call_action("check_voucher", payload, client=client, method="POST")
    if root is None:
        raise TerrawindEmissionError(502, "Terrawind check_voucher sin respuesta")
    status = (root.findtext("voucher_status") or "").strip().upper()
    if status != "OK":
        raise TerrawindEmissionError(400, "Terrawind rechazó check_voucher")


def emit_terrawind_vouchers(
    *,
    product_id: str,
    country_id: str,
    departure_date: date,
    return_date: date,
    passengers: list[TerrawindPassengerDraft],
    voucher_int_ref_prefix: str | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> TerrawindEmissionResult:
    """Emite un voucher por pasajero vía add_vouchers."""
    if not passengers:
        raise TerrawindEmissionError(400, "No hay pasajeros para emitir")
    if len(passengers) > 20:
        raise TerrawindEmissionError(400, "Máximo 20 pasajeros por emisión Terrawind")

    form: dict[str, str] = {}
    for index, passenger in enumerate(passengers):
        ref = None
        if voucher_int_ref_prefix:
            ref = f"{voucher_int_ref_prefix}-{index + 1}"[:80]
        form.update(
            _passenger_payload(
                product_id=product_id,
                country_id=country_id,
                departure_date=departure_date,
                return_date=return_date,
                passenger=passenger,
                voucher_int_ref=ref,
                index=index,
            )
        )

    root = _call_action(
        "add_vouchers",
        form,
        client=client,
        method="POST",
        settings=settings,
    )
    if root is None:
        raise TerrawindEmissionError(502, "Terrawind add_vouchers sin respuesta")

    issued: list[TerrawindIssuedVoucher] = []
    for node in root.findall("voucher"):
        fields = _xml_child_map(node)
        number = fields.get("voucher_number", "").strip()
        key = fields.get("voucher_key", "").strip()
        if not number or not key:
            continue
        issued.append(
            TerrawindIssuedVoucher(
                voucher_number=number,
                voucher_key=key,
                passenger_document_number=fields.get("passenger_document_number"),
                voucher_int_ref=fields.get("voucher_int_ref"),
                pdf_url=voucher_pdf_url(number, key, settings=settings),
            )
        )

    status = (root.findtext("voucher_status") or "").strip().upper()
    if status != "OK" or not issued:
        err = TerrawindEmissionError(502, "Terrawind no devolvió vouchers emitidos")
        err.request_body = form
        err.response_body = {"voucher_status": status, "count": len(issued)}
        raise err

    return TerrawindEmissionResult(
        vouchers=issued,
        status_code=200,
        request_body=form,
        response_body={"voucher_status": status, "count": len(issued)},
    )


def _passenger_payload(
    *,
    product_id: str,
    country_id: str,
    departure_date: date,
    return_date: date,
    passenger: TerrawindPassengerDraft,
    voucher_int_ref: str | None,
    index: int | None,
) -> dict[str, str]:
    prefix = f"vouchers[{index}]" if index is not None else ""

    def key(name: str) -> str:
        return f"{prefix}[{name}]" if prefix else name

    gender = normalize_gender(passenger.gender)
    payload = {
        key("voucher_date_from"): format_setw_date(departure_date),
        key("voucher_date_to"): format_setw_date(return_date),
        key("voucher_product_id"): str(product_id).strip(),
        key("country_id"): str(country_id).strip(),
        key("passenger_document_type_id"): document_type_id(passenger.document_type),
        key("passenger_document_number"): passenger.document_number.strip(),
        key("passenger_birth_date"): format_setw_date(passenger.birth_date),
        key("passenger_gender"): gender,
        key("passenger_first_name"): passenger.first_name.strip(),
        key("passenger_last_name"): passenger.last_name.strip(),
        key("passenger_emergency_first_name"): passenger.emergency_first_name.strip(),
        key("passenger_emergency_last_name"): passenger.emergency_last_name.strip(),
        key("passenger_emergency_phone_1"): passenger.emergency_phone_1.strip(),
    }
    if voucher_int_ref:
        payload[key("voucher_int_ref")] = voucher_int_ref
    if passenger.second_name:
        payload[key("passenger_second_name")] = passenger.second_name.strip()
    if passenger.residence_country_id:
        payload[key("passenger_country_id")] = passenger.residence_country_id.strip()
    if passenger.city:
        payload[key("passenger_city")] = passenger.city.strip()
    if passenger.address:
        payload[key("passenger_address")] = passenger.address.strip()
    if passenger.phone:
        payload[key("passenger_phone")] = passenger.phone.strip()
    if passenger.email:
        payload[key("passenger_email")] = passenger.email.strip()
    if passenger.emergency_phone_2:
        payload[key("passenger_emergency_phone_2")] = passenger.emergency_phone_2.strip()
    return payload


def _call_action(
    action: str,
    params: dict[str, str],
    *,
    client: httpx.Client | None = None,
    method: str = "GET",
    settings: Settings | None = None,
) -> ET.Element | None:
    settings = settings or get_settings()
    if not settings.terrawind_user.strip() or not settings.terrawind_password.strip():
        raise TerrawindEmissionError(500, "Faltan TERRAWIND_USER / TERRAWIND_PASSWORD")

    base_url = settings.terrawind_base_url.strip()
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=_HTTP_TIMEOUT,
            auth=(settings.terrawind_user, settings.terrawind_password),
            follow_redirects=True,
        )
    try:
        request_params = {"action": action, **params}
        if method.upper() == "POST":
            response = client.post(base_url, data=request_params)
        else:
            response = client.get(base_url, params=request_params)

        if response.status_code >= 400:
            logger.error(
                "Terrawind: action=%s HTTP %s - body=%s",
                action,
                response.status_code,
                response.text[:800],
            )
            err = TerrawindEmissionError(
                502,
                f"Terrawind {action} HTTP {response.status_code}",
            )
            err.http_status = response.status_code
            err.request_body = request_params
            err.response_body = {"raw": response.text[:800]}
            raise err

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            logger.error("Terrawind: XML inválido action=%s body=%s", action, response.text[:800])
            err = TerrawindEmissionError(502, f"Terrawind {action}: XML inválido")
            err.http_status = response.status_code
            err.request_body = request_params
            err.response_body = {"raw": response.text[:800]}
            raise err from exc

        error = root.find("error")
        if error is not None:
            number = (error.findtext("error_number") or "").strip()
            description = (error.findtext("error_description") or "").strip()
            detail = description or f"Terrawind error {number or 'desconocido'}"
            logger.warning(
                "Terrawind: action=%s error=%s desc=%s",
                action,
                number,
                description,
            )
            err = TerrawindEmissionError(400 if number not in {"0", "1", "2"} else 502, detail)
            err.http_status = response.status_code
            err.request_body = request_params
            err.response_body = {"error_number": number, "error_description": description}
            raise err
        return root
    finally:
        if owns_client:
            client.close()


def _xml_child_map(node: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in list(node):
        result[child.tag] = (child.text or "").strip()
    return result
