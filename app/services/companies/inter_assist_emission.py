"""Emisión InterAssist vía POST /api/ventas/emitir-externa.

No cobra: el cobro lo hace nuestra pasarela. Este módulo solo emite vouchers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
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
    if not settings.interassist_api_key.strip():
        raise InterAssistEmissionError(400, "Falta INTERASSIST_API_KEY")

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
    if not settings.interassist_api_key.strip():
        raise InterAssistEmissionError(400, "Falta INTERASSIST_API_KEY")
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
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(item) for item in errors[:5])
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:5])
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
