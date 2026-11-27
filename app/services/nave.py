"""Cliente HTTP de Nave Checkout: auth M2M, intención de pago y consulta de estado."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_TOKEN_SAFETY_MARGIN_SECONDS = 60
_MAX_RETRIES = 3


class NaveError(Exception):
    """Error al hablar con la API de Nave."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.response_body = response_body


@dataclass(frozen=True)
class NavePaymentLink:
    payment_request_id: str
    checkout_url: str
    external_payment_id: str
    raw: dict[str, Any]


class NaveTokenManager:
    """Cache en memoria del access_token Nave (M2M JSON body)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self, client: httpx.Client, settings: Settings) -> str:
        now = time.monotonic()
        if self._token and now < self._expires_at - _TOKEN_SAFETY_MARGIN_SECONDS:
            return self._token
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._expires_at - _TOKEN_SAFETY_MARGIN_SECONDS:
                return self._token
            return self._refresh_locked(client, settings)

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def force_refresh(self, client: httpx.Client, settings: Settings) -> str:
        with self._lock:
            return self._refresh_locked(client, settings)

    def _refresh_locked(self, client: httpx.Client, settings: Settings) -> str:
        if not settings.nave_client_id.strip() or not settings.nave_client_secret.strip():
            raise NaveError(500, "Faltan NAVE_CLIENT_ID / NAVE_CLIENT_SECRET")

        body = {
            "client_id": settings.nave_client_id.strip(),
            "client_secret": settings.nave_client_secret.strip(),
            "audience": settings.nave_audience.strip(),
        }
        response = client.post(
            settings.nave_auth_url.strip(),
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            logger.error(
                "Nave: auth HTTP %s - body=%s",
                response.status_code,
                response.text[:500],
            )
            raise NaveError(
                502,
                f"Nave auth falló con HTTP {response.status_code}",
                response_body=_safe_json(response),
            )

        data = _safe_json(response) or {}
        token = data.get("access_token")
        if not token:
            raise NaveError(502, "Nave auth sin access_token", response_body=data)

        expires_in = int(data.get("expires_in") or 86400)
        self._token = str(token)
        self._expires_at = time.monotonic() + expires_in
        logger.info("Nave: token renovado expires_in=%ss", expires_in)
        return self._token


_token_manager = NaveTokenManager()


def create_payment_request(
    *,
    external_payment_id: str,
    amount: Decimal,
    product_name: str,
    product_description: str | None = None,
    buyer: dict[str, Any] | None = None,
    callback_url: str | None = None,
    notification_url: str | None = None,
    duration_time: int | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> NavePaymentLink:
    """Crea una intención de pago y devuelve el checkout_url."""
    settings = settings or get_settings()
    pos_id = settings.nave_pos_id.strip()
    if not pos_id:
        raise NaveError(500, "Falta NAVE_POS_ID")

    amount_value = _money(amount)
    product: dict[str, Any] = {
        "name": product_name,
        "quantity": 1,
        "unit_price": {"currency": "ARS", "value": amount_value},
    }
    if product_description:
        product["description"] = product_description

    body: dict[str, Any] = {
        "external_payment_id": external_payment_id[:36],
        "seller": {"pos_id": pos_id},
        "transactions": [
            {
                "amount": {"currency": "ARS", "value": amount_value},
                "products": [product],
            }
        ],
    }

    resolved_callback = (callback_url or settings.nave_callback_url or "").strip()
    resolved_notification = (
        notification_url or settings.nave_notification_url or ""
    ).strip()
    additional_info: dict[str, str] = {}
    if resolved_callback:
        additional_info["callback_url"] = resolved_callback
    if resolved_notification:
        additional_info["notification_url"] = resolved_notification
    if additional_info:
        body["additional_info"] = additional_info

    if buyer:
        body["buyer"] = buyer
    if duration_time is not None:
        body["duration_time"] = duration_time

    data = _request_json(
        "POST",
        f"{settings.nave_api_base_url.rstrip('/')}/payment_requests/ecommerce",
        json_body=body,
        client=client,
        settings=settings,
    )
    payment_request_id = str(data.get("id") or "").strip()
    checkout_url = str(data.get("checkout_url") or "").strip()
    if not payment_request_id or not checkout_url:
        raise NaveError(
            502,
            "Respuesta de Nave incompleta (faltan id o checkout_url)",
            response_body=data,
        )
    return NavePaymentLink(
        payment_request_id=payment_request_id,
        checkout_url=checkout_url,
        external_payment_id=external_payment_id[:36],
        raw=data,
    )


def get_payment_by_check_url(
    payment_check_url: str,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Consulta el estado del pago usando la URL del webhook."""
    url = payment_check_url.strip()
    if not url.startswith("https://") and not url.startswith("http://"):
        url = "https://" + url.lstrip("/")
    return _request_json(
        "GET",
        url,
        client=client,
        settings=settings or get_settings(),
    )


def get_payment_request(
    payment_request_id: str,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Consulta el estado de una intención de pago."""
    settings = settings or get_settings()
    request_id = payment_request_id.strip()
    if not request_id:
        raise NaveError(400, "Falta payment_request_id")
    return _request_json(
        "GET",
        f"{settings.nave_api_base_url.rstrip('/')}/payment_requests/{request_id}",
        client=client,
        settings=settings,
    )


def extract_nave_status(payload: dict[str, Any]) -> str | None:
    """Normaliza el estado de Nave a approved|pending|rejected|refunded|None."""
    status_field = payload.get("status")
    raw: str | None = None
    if isinstance(status_field, dict):
        name = status_field.get("name") or status_field.get("code")
        raw = str(name) if name is not None else None
    elif status_field is not None:
        raw = str(status_field)
    else:
        for key in ("payment_status", "state"):
            value = payload.get(key)
            if value is not None:
                raw = str(value)
                break
    if not raw:
        return None
    normalized = raw.strip().upper()
    if normalized in {"APPROVED", "PAID", "ACCREDITED", "SUCCESS"}:
        return "approved"
    if normalized in {"PENDING", "IN_PROCESS", "IN_PROGRESS", "CREATED"}:
        return "pending"
    if normalized in {"REJECTED", "DECLINED", "CANCELLED", "CANCELED", "FAILED"}:
        return "rejected"
    if normalized in {"REFUNDED", "CHARGEBACK"}:
        return "refunded"
    return normalized.lower()


def _request_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    settings: Settings,
) -> dict[str, Any]:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        last_error: NaveError | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            token = _token_manager.get_token(client, settings)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            response = client.request(method, url, headers=headers, json=json_body)

            if response.status_code == 401 and attempt == 1:
                logger.warning("Nave: 401; renovando token")
                _token_manager.force_refresh(client, settings)
                continue

            if response.status_code >= 500 and attempt < _MAX_RETRIES:
                logger.warning(
                    "Nave: HTTP %s intento %s; reintentando",
                    response.status_code,
                    attempt,
                )
                time.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                body = _safe_json(response)
                logger.error(
                    "Nave: %s %s HTTP %s - body=%s",
                    method,
                    url,
                    response.status_code,
                    response.text[:800],
                )
                last_error = NaveError(
                    502,
                    f"Nave respondió HTTP {response.status_code}",
                    response_body=body,
                )
                raise last_error

            data = _safe_json(response)
            if not isinstance(data, dict):
                raise NaveError(502, "Respuesta Nave no es un objeto JSON")
            return data

        raise last_error or NaveError(502, "Máximo de reintentos contra Nave")
    finally:
        if owns_client:
            client.close()


def _money(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    return f"{quantized:.2f}"


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
