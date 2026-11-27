"""Alta de voucher Universal. El cobro lo hace la pasarela simulada."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.core.config import get_settings, get_universal_destinos
from app.services.companies.universal import universal_coverage_end

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 60.0
_NATIONAL_DESTINATION = 1
_RESIDENCE = "ARGENTINA"
_CHANNEL = "Turismo"
_DOCUMENT_TYPES = {
    "dni": "DNI",
    "passport": "Pasaporte",
    "cuit": "Otros",
}
_ALTA_ACTION = "document/http://siebel.com/CustomUI:Alta_Voucher_Operation"
_BAJA_ACTION = "document/http://siebel.com/marketing/leads:LeadServiceRetireLead"
_PDF_ACTION = "document.rpc/http://siebel.com/CustomUI:SendReportOper"
_LANG = {"es": "ESN", "en": "ENU", "pt": "PTB"}


class UniversalEmissionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.http_status: int | None = None
        self.request_body: dict[str, Any] | None = None
        self.response_body: dict[str, Any] | None = None


@dataclass
class UniversalPassengerDraft:
    first_name: str
    last_name: str
    birth_date: date
    document_type: str
    document_number: str
    email: str | None


@dataclass
class UniversalVoucher:
    number: str
    status_code: int
    request_body: dict[str, Any]
    response_body: dict[str, Any]


def universal_charge(
    destination_id: int,
    amount_usd: Decimal,
    amount_local: Decimal | None,
) -> tuple[str, Decimal, str]:
    """Moneda de lista, precio y destino SOAP. Nacional cobra en pesos."""
    destinos = get_universal_destinos(get_settings())
    destino = destinos.get(destination_id)
    if not destino:
        raise UniversalEmissionError(400, "Ese destino no está habilitado para Universal")
    if destination_id == _NATIONAL_DESTINATION:
        if amount_local is None or amount_local <= 0:
            raise UniversalEmissionError(
                400,
                "El destino nacional requiere el precio en pesos",
            )
        return "ARS", amount_local, destino
    if amount_usd <= 0:
        raise UniversalEmissionError(400, "Falta el precio en dólares")
    return "USD", amount_usd, destino


def document_type_name(document_type: str) -> str:
    mapped = _DOCUMENT_TYPES.get(document_type.strip().lower())
    if mapped is None:
        raise UniversalEmissionError(400, "El tipo de documento no es válido para Universal")
    return mapped


def emit_universal_voucher(
    *,
    lead_id: str,
    control_number: str,
    organization_id: str,
    plan_name: str,
    destination: str,
    currency: str,
    amount: Decimal,
    departure_date: date,
    return_date: date,
    trip_type: str,
    passengers: list[UniversalPassengerDraft],
    client: httpx.Client | None = None,
) -> UniversalVoucher:
    settings = get_settings()
    if not settings.universal_username or not settings.universal_password:
        raise UniversalEmissionError(502, "Faltan las credenciales de Universal")
    if not settings.universal_convenio.strip():
        raise UniversalEmissionError(502, "Falta el convenio de Universal")
    if not organization_id.strip():
        raise UniversalEmissionError(400, "Falta la organización de Universal")
    coverage_end = universal_coverage_end(trip_type, departure_date, return_date)
    issued_on = date.today()
    request_body = {
        "lead_id": lead_id,
        "control_number": control_number,
        "organization_id": organization_id,
        "plan_name": plan_name,
        "destination": destination,
        "currency": currency,
        "amount": str(amount),
        "departure_date": departure_date.isoformat(),
        "return_date": coverage_end.isoformat(),
        "passengers": len(passengers),
    }
    envelope = _alta_envelope(
        username=settings.universal_username,
        password=settings.universal_password,
        convenio=settings.universal_convenio.strip(),
        lead_id=lead_id,
        control_number=control_number,
        organization_id=organization_id.strip(),
        plan_name=plan_name,
        destination=destination,
        currency=currency,
        amount=amount,
        issued_on=issued_on,
        departure_date=departure_date,
        return_date=coverage_end,
        passengers=passengers,
    )
    try:
        raw, status_code = _post(settings.universal_soap_url, envelope, _ALTA_ACTION, client)
    except UniversalEmissionError as exc:
        exc.request_body = request_body
        raise
    root = _parse_xml(raw)
    error = _text(root, "ErrorMsg")
    number = _text(root, "NroVoucher")
    response_body = {"error": error, "voucher_number": number, "body": raw[:4000]}
    if error.upper() != "OK" or not number:
        exc = UniversalEmissionError(502, error or "Universal no emitió el voucher")
        exc.http_status = status_code
        exc.request_body = request_body
        exc.response_body = response_body
        raise exc
    return UniversalVoucher(
        number=number,
        status_code=status_code,
        request_body=request_body,
        response_body=response_body,
    )


def retire_universal_lead(lead_id: str, client: httpx.Client | None = None) -> None:
    """Da de baja el lead después de emitir. No deshace el voucher si falla."""
    settings = get_settings()
    envelope = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:lead="http://siebel.com/marketing/leads">'
        "<soapenv:Header>"
        '<wsse:Security xmlns:wsse="http://schemas.xmlsoap.org/ws/2002/07/secext">'
        '<wsse:UsernameToken xmlns:wsu="http://schemas.xmlsoap.org/ws/2002/07/utility">'
        f"<wsse:Username>{_xml(settings.universal_username)}</wsse:Username>"
        f'<wsse:Password Type="wsse:PasswordText">{_xml(settings.universal_password)}</wsse:Password>'
        "</wsse:UsernameToken></wsse:Security></soapenv:Header><soapenv:Body>"
        "<lead:LeadServiceRetireLead_Input>"
        "<lead:Comments></lead:Comments>"
        "<lead:ReasonCode>Venta Online</lead:ReasonCode>"
        f"<lead:LeadId>{_xml(lead_id)}</lead:LeadId>"
        "</lead:LeadServiceRetireLead_Input></soapenv:Body></soapenv:Envelope>"
    )
    _post(settings.universal_soap_url, envelope, _BAJA_ACTION, client)


def fetch_universal_voucher_pdf(
    voucher_number: str,
    organization_id: str,
    lang: str = "es",
    client: httpx.Client | None = None,
) -> bytes:
    settings = get_settings()
    language = _LANG.get(lang, "ESN")
    envelope = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:cus="http://siebel.com/CustomUI"><soapenv:Header>'
        '<wsse:Security xmlns:wsse="http://schemas.xmlsoap.org/ws/2002/07/secext">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{_xml(settings.universal_username)}</wsse:Username>"
        f'<wsse:Password Type="wsse:PasswordText">{_xml(settings.universal_password)}</wsse:Password>'
        "</wsse:UsernameToken></wsse:Security></soapenv:Header><soapenv:Body>"
        "<cus:SendReportOper>"
        f"<Language>{language}</Language>"
        f"<VoucherNumber>{_xml(voucher_number)}</VoucherNumber>"
        "<Tarifa>N</Tarifa>"
        f"<Organization>{_xml(organization_id)}</Organization>"
        "</cus:SendReportOper></soapenv:Body></soapenv:Envelope>"
    )
    raw, status_code = _post(settings.universal_soap_url, envelope, _PDF_ACTION, client)
    root = _parse_xml(raw)
    encoded = _text(root, "ReportOutputFileBuffer")
    if not encoded:
        exc = UniversalEmissionError(502, "Universal no devolvió el PDF del voucher")
        exc.http_status = status_code
        exc.response_body = {"body": raw[:4000]}
        raise exc
    try:
        return base64.b64decode(encoded, validate=False)
    except ValueError as exc:
        raise UniversalEmissionError(502, "El PDF de Universal no es válido") from exc


def _alta_envelope(
    *,
    username: str,
    password: str,
    convenio: str,
    lead_id: str,
    control_number: str,
    organization_id: str,
    plan_name: str,
    destination: str,
    currency: str,
    amount: Decimal,
    issued_on: date,
    departure_date: date,
    return_date: date,
    passengers: list[UniversalPassengerDraft],
) -> str:
    solicitantes = "".join(_solicitante(passenger) for passenger in passengers)
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:cus="http://siebel.com/CustomUI" '
        'xmlns:ua="http://www.siebel.com/xml/UA%20Alta%20Vouche%20Min%20Request">'
        "<soapenv:Header>"
        '<wsse:Security xmlns:wsse="http://schemas.xmlsoap.org/ws/2002/07/secext">'
        '<wsse:UsernameToken xmlns:wsu="http://schemas.xmlsoap.org/ws/2002/07/utility">'
        f"<wsse:Username>{_xml(username)}</wsse:Username>"
        f'<wsse:Password Type="wsse:PasswordText">{_xml(password)}</wsse:Password>'
        "</wsse:UsernameToken></wsse:Security></soapenv:Header><soapenv:Body>"
        "<cus:Alta_Voucher_Operation_Input><ua:UAAltaVoucheMinRequest><ua:DatosVoucher>"
        f"<ua:NroControl>{_xml(control_number)}</ua:NroControl>"
        f"<ua:Vendedor>{_xml(username)}</ua:Vendedor>"
        f"<ua:FechaEmision>{_soap_date(issued_on)}</ua:FechaEmision>"
        f"<ua:Destino>{_xml(destination)}</ua:Destino>"
        f"<ua:FechaVigencia>{_soap_date(departure_date)}</ua:FechaVigencia>"
        f"<ua:FechaFinal>{_soap_date(return_date)}</ua:FechaFinal>"
        f"<ua:MonedaLista>{_xml(currency)}</ua:MonedaLista>"
        f"<ua:Precio>{amount}</ua:Precio>"
        f"<ua:Canal>{_CHANNEL}</ua:Canal>"
        f"<ua:Contrato>{_xml(convenio)}</ua:Contrato>"
        f"<ua:LeadId>{_xml(lead_id)}</ua:LeadId>"
        "<ua:EnvioVoucherMail>N</ua:EnvioVoucherMail>"
        "<ua:PostProcesoFlag>Y</ua:PostProcesoFlag>"
        "<ua:DatosAgencia>"
        f"<ua:OrganizacionRegistradora>{_xml(organization_id)}</ua:OrganizacionRegistradora>"
        "</ua:DatosAgencia><ua:DatosProducto>"
        f"<ua:NombreProducto>{_xml(plan_name)}</ua:NombreProducto>"
        f"</ua:DatosProducto>{solicitantes}"
        "</ua:DatosVoucher></ua:UAAltaVoucheMinRequest>"
        "</cus:Alta_Voucher_Operation_Input></soapenv:Body></soapenv:Envelope>"
    )


def _solicitante(passenger: UniversalPassengerDraft) -> str:
    email = f"<ua:CorreoElectronicoSolicitante>{_xml(passenger.email)}</ua:CorreoElectronicoSolicitante>" if passenger.email else ""
    return (
        "<ua:DatosSolicitante>"
        f"<ua:NombreSolicitante>{_xml(passenger.first_name)}</ua:NombreSolicitante>"
        f"<ua:ApellidoSolicitante>{_xml(passenger.last_name)}</ua:ApellidoSolicitante>"
        f"<ua:TipoDocumentoSolicitante>{_xml(document_type_name(passenger.document_type))}</ua:TipoDocumentoSolicitante>"
        f"<ua:NroDocumentoSolicitante>{_xml(passenger.document_number)}</ua:NroDocumentoSolicitante>"
        f"<ua:PaisResidenciaSolicitante>{_RESIDENCE}</ua:PaisResidenciaSolicitante>"
        f"<ua:FechaNacimientoSolicitante>{_soap_date(passenger.birth_date)}</ua:FechaNacimientoSolicitante>"
        f"{email}</ua:DatosSolicitante>"
    )


def _post(
    url: str,
    envelope: str,
    action: str,
    client: httpx.Client | None,
) -> tuple[str, int]:
    headers = {
        "Content-Type": "text/xml;charset=UTF-8",
        "Accept": "text/xml",
        "SOAPAction": f'"{action}"',
        "User-Agent": "Cotizador-API/1.0",
    }
    own_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        response = http.post(url, content=envelope.encode("utf-8"), headers=headers)
    except httpx.HTTPError as exc:
        raise UniversalEmissionError(502, "Universal no respondió") from exc
    finally:
        if own_client:
            http.close()
    if response.status_code >= 400:
        exc = UniversalEmissionError(502, "Universal rechazó la solicitud")
        exc.http_status = response.status_code
        exc.response_body = {"body": response.text[:4000]}
        raise exc
    return response.text, response.status_code


def _parse_xml(raw: str) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UniversalEmissionError(502, "Universal devolvió un XML inválido") from exc


def _text(root: ET.Element, name: str) -> str:
    for node in root.iter():
        if _local(node.tag) == name and (node.text or "").strip():
            return (node.text or "").strip()
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _soap_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
