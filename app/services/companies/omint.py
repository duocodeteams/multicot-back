"""Adaptador para Omint Assistance (B2B CreateQuotationB2B).

Auth: OAuth2 Client Credentials (token de agencia, no por usuario).
Cache en memoria del proceso con margen de 5 minutos y retry único ante 401.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.quotations.schemas import (
    DESTINO_ID_EUROPA,
    DESTINO_ID_LATINOAMERICA,
    DESTINO_ID_NACIONAL,
    DESTINO_ID_NORTEAMERICA,
    DESTINO_ID_RESTO_MUNDO,
    TRIP_TYPE_LARGA_ESTADIA,
    TRIP_TYPE_MULTIVIAJE,
    TRIP_TYPE_UNICO_VIAJE,
    Benefit,
    QuotePlan,
    QuoteRequest,
)

logger = logging.getLogger(__name__)

_DEPARTURE_CODE_AR = "MAA"
_TARIFF_TYPE_BRUTO = "B"
_TOKEN_SAFETY_MARGIN_SECONDS = 5 * 60
_HTTP_TIMEOUT = 30.0

# destination_id (1-5) → destinationCode Omint
_DESTINATION_MAP: dict[int, str] = {
    DESTINO_ID_NACIONAL: "ARG",
    DESTINO_ID_LATINOAMERICA: "ASU",
    DESTINO_ID_EUROPA: "EMO",
    DESTINO_ID_RESTO_MUNDO: "MUC",
    DESTINO_ID_NORTEAMERICA: "NAC",
}

# trip_type → productTypeCode Omint
_PRODUCT_TYPE_MAP: dict[str, str] = {
    TRIP_TYPE_UNICO_VIAJE: "S",
    TRIP_TYPE_MULTIVIAJE: "A",
    TRIP_TYPE_LARGA_ESTADIA: "L",
}

# PA-S-d050-Cov / PA-S-e040-Cov → miles de cobertura (50 → 50000)
_COVERAGE_FROM_CODE_RE = re.compile(r"[de](\d{3})", re.IGNORECASE)


class OmintTokenManager:
    """Cache en memoria del access_token Omint (Client Credentials)."""

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
        body = {
            "grant_type": "client_credentials",
            "client_id": settings.omint_client_id,
            "client_secret": settings.omint_client_secret,
            "scope": settings.omint_scope,
        }
        t0 = time.monotonic()
        response = client.post(
            settings.omint_token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if response.status_code >= 400:
            logger.error(
                "Omint: /connect/token HTTP %s (%sms) - body=%s",
                response.status_code,
                elapsed_ms,
                response.text[:500],
            )
            response.raise_for_status()

        data = response.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in") or 3600)
        if not token:
            raise RuntimeError("Omint: respuesta de token sin access_token")

        self._token = str(token)
        self._expires_at = time.monotonic() + expires_in
        logger.info(
            "Omint: token renovado expires_in=%ss (utc=%s)",
            expires_in,
            datetime.now(timezone.utc).isoformat(),
        )
        return self._token


_token_manager = OmintTokenManager()


class OmintQuoteProvider:
    company_name = "Omint"
    company_slug = "omint"
    
    def __init__(self) -> None:
        self._settings = get_settings()
        self._token_manager = _token_manager

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.omint_client_id or not settings.omint_client_secret:
            logger.debug("Omint: faltan OMINT_CLIENT_ID/OMINT_CLIENT_SECRET")
            return []

        if request.origin != "AR":
            logger.debug("Omint: origen '%s' no soportado (solo AR)", request.origin)
            return []

        destination_code = _DESTINATION_MAP.get(request.destination_id)
        if not destination_code:
            logger.debug(
                "Omint: destination_id=%s sin mapeo",
                request.destination_id,
            )
            return []

        product_type_code = _PRODUCT_TYPE_MAP.get(request.trip_type)
        if not product_type_code:
            logger.debug("Omint: trip_type='%s' sin mapeo", request.trip_type)
            return []

        body = self._build_request_body(
            request=request,
            destination_code=destination_code,
            product_type_code=product_type_code,
        )

        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            try:
                data = self._create_quotation(client, settings, body, retry_on_401=True)
            except httpx.HTTPError:
                logger.exception("Omint: error HTTP en CreateQuotationB2B")
                return []
            except Exception:
                logger.exception("Omint: error inesperado en CreateQuotationB2B")
                return []

        return self._parse_products(data)

    def _build_request_body(
        self,
        request: QuoteRequest,
        destination_code: str,
        product_type_code: str,
    ) -> dict[str, Any]:
        settings = self._settings
        date_since = datetime.combine(
            request.departure_date, datetime.min.time(), tzinfo=timezone.utc
        )
        date_until = datetime.combine(
            request.return_date, datetime.min.time(), tzinfo=timezone.utc
        )
        body: dict[str, Any] = {
            "productTypeCode": product_type_code,
            "tariffTypeCode": _TARIFF_TYPE_BRUTO,
            "departureCode": _DEPARTURE_CODE_AR,
            "destinationCode": destination_code,
            "dateSince": date_since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dateUntil": date_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "passengerAges": list(request.ages),
            "email": settings.cotizador_default_email,
        }
        if product_type_code == "A":
            body["quantityOfDays"] = settings.omint_annual_quantity_of_days
        if settings.omint_agreement_number is not None:
            body["agreementNumber"] = settings.omint_agreement_number
        if settings.omint_market_id:
            body["marketId"] = settings.omint_market_id
        return body

    def _create_quotation(
        self,
        client: httpx.Client,
        settings: Settings,
        body: dict[str, Any],
        *,
        retry_on_401: bool,
    ) -> dict[str, Any]:
        token = self._token_manager.get_token(client, settings)
        t0 = time.monotonic()
        response = client.post(
            settings.omint_quote_url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if response.status_code == 401 and retry_on_401:
            logger.warning("Omint: 401 en cotización; renovando token y reintentando")
            self._token_manager.force_refresh(client, settings)
            return self._create_quotation(
                client, settings, body, retry_on_401=False
            )

        if response.status_code >= 400:
            logger.error(
                "Omint: CreateQuotationB2B HTTP %s (%sms) - body=%s request=%s",
                response.status_code,
                elapsed_ms,
                response.text[:800],
                body,
            )
            response.raise_for_status()

        logger.debug(
            "Omint: CreateQuotationB2B HTTP %s (%sms)",
            response.status_code,
            elapsed_ms,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Omint: respuesta inesperada type={type(data).__name__}"
            )
        return data

    def _parse_products(self, data: dict[str, Any]) -> list[QuotePlan]:
        quotation_id = str(data.get("id") or "")
        products = data.get("products") or []
        if not isinstance(products, list):
            logger.warning("Omint: products no es lista")
            return []

        plans: list[QuotePlan] = []
        for product in products:
            if not isinstance(product, dict) or not product:
                continue
            plan = self._product_to_quote_plan(quotation_id, product)
            if plan is not None:
                plans.append(plan)

        logger.debug("Omint: quote ok plans=%s", len(plans))
        return plans

    def _product_to_quote_plan(
        self, quotation_id: str, product: dict[str, Any]
    ) -> QuotePlan | None:
        product_code = (product.get("productCode") or "").strip()
        product_id = str(product.get("id") or product_code or "")
        denomination = (product.get("denomination") or "").strip()
        if not product_code and not product_id:
            return None

        # API real: basePrice / finalPrice (strings formato AR).
        # El manual menciona netPrice/grossPrice; se aceptan como fallback.
        base_rate = _parse_omint_amount(
            product.get("basePrice")
            if product.get("basePrice") not in (None, "")
            else product.get("grossPrice")
        )
        final_rate = _parse_omint_amount(
            product.get("finalPrice")
            if product.get("finalPrice") not in (None, "")
            else product.get("netPrice")
        )
        if final_rate <= 0 and base_rate <= 0:
            logger.debug(
                "Omint: producto sin precios productCode=%s keys=%s",
                product_code,
                sorted(product.keys()),
            )
            return None

        if final_rate <= 0:
            final_rate = base_rate
        # Omint no informa tarifa en USD ni FX; no inventar valores.
        exchange_rate = Decimal("1")
        # net_rate: precio final (no hay neto de comisión explícito en B2B).
        net_rate = final_rate

        discount_pct = _parse_omint_amount(product.get("promotionPercentage"))
        if discount_pct <= 0:
            discount_pct = None

        services = product.get("services") or []
        coverage_amount = _coverage_from_services(services)
        if coverage_amount <= 0:
            coverage_amount = _coverage_from_product_code(product_code)
        benefits = _services_to_benefits(services)

        plan_id = product_code or product_id
        quote_id = f"{quotation_id}|{product_id}" if quotation_id else product_id

        return QuotePlan(
            company=self.company_name,
            id=quote_id,
            plan_id=plan_id,
            plan_name=denomination or plan_id,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=net_rate.quantize(Decimal("0.01")),
            final_rate_usd=None,
            exchange_rate=exchange_rate,
            final_rate=final_rate.quantize(Decimal("0.01")),
            base_rate_usd=None,
            base_rate=(
                base_rate.quantize(Decimal("0.01")) if base_rate > 0 else None
            ),
            discount_pct=(
                discount_pct.quantize(Decimal("0.01")) if discount_pct is not None else None
            ),
        )


def _parse_omint_amount(value: Any) -> Decimal:
    """Parsea montos Omint: '210.672' / '210.672,00' / '50.000' / 17556.0."""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    s = str(value).strip()
    if not s:
        return Decimal("0")
    # Formato AR: miles con '.' y decimales con ',' (ej. 210.672,00).
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1:
        left, right = s.split(".")
        # '351.120' / '50.000' → miles; '12.5' → decimal.
        if len(right) == 3 and left.isdigit() and right.isdigit():
            s = left + right
    else:
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _coverage_from_services(services: Any) -> Decimal:
    """Toma el tope de asistencia médica principal (Accidente/Enfermedad)."""
    if not isinstance(services, list):
        return Decimal("0")
    for service in services:
        if not isinstance(service, dict):
            continue
        description = (service.get("description") or "").lower()
        if "accidente" in description and "enfermedad" in description:
            amount = _parse_omint_amount(service.get("maxAmount"))
            if amount > 0:
                return amount
    return Decimal("0")


def _services_to_benefits(services: Any) -> list[Benefit]:
    if not isinstance(services, list):
        return []
    benefits: list[Benefit] = []
    for idx, service in enumerate(services):
        if not isinstance(service, dict):
            continue
        nombre = (service.get("description") or "").strip()
        if not nombre:
            continue
        valor = (service.get("coverageLimit") or "").strip()
        if not valor:
            max_amount = (service.get("maxAmount") or "").strip()
            currency = (service.get("currency") or "").strip()
            valor = f"{currency} {max_amount}".strip() if max_amount else ""
        benefits.append(Benefit(id=idx + 1, nombre=nombre, valor=valor or "Incluido"))
    return benefits


def _coverage_from_product_code(product_code: str) -> Decimal:
    """PA-S-d050-Cov → 50000; PA-S-e040-Cov → 40000."""
    if not product_code:
        return Decimal("0")
    match = _COVERAGE_FROM_CODE_RE.search(product_code)
    if not match:
        return Decimal("0")
    try:
        thousands = int(match.group(1))
    except ValueError:
        return Decimal("0")
    if thousands <= 0:
        return Decimal("0")
    return Decimal(thousands * 1000)
