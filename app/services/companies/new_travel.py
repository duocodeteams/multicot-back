"""Adaptador para New Travel (cotización por /orders/quote + beneficios por plan)."""

import logging
import time
from decimal import Decimal
from typing import Any

import httpx
import re

from app.core.config import get_settings
from app.quotations.schemas import (
    DESTINO_ID_EUROPA,
    DESTINO_ID_LATINOAMERICA,
    DESTINO_ID_NACIONAL,
    DESTINO_ID_NORTEAMERICA,
    DESTINO_ID_RESTO_MUNDO,
    Benefit,
    QuotePlan,
    QuoteRequest,
)

logger = logging.getLogger(__name__)

# destination_id (cotizador) -> idTerritory de New Travel
# 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica
_DESTINATION_TO_TERRITORY: dict[int, str] = {
    DESTINO_ID_NACIONAL: "9",  # Local
    DESTINO_ID_LATINOAMERICA: "1",  # Mundial
    DESTINO_ID_EUROPA: "2",  # Europa
    DESTINO_ID_RESTO_MUNDO: "1",  # Mundial
    DESTINO_ID_NORTEAMERICA: "1",  # Mundial
}


class NewTravelQuoteProvider:
    company_name = "New Travel"

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.new_travel_user or not settings.new_travel_password:
            logger.debug(
                "NewTravel: faltan NEW_TRAVEL_USER/NEW_TRAVEL_PASSWORD configurados"
            )
            return []

        base_url = settings.new_travel_base_url.rstrip("/")
        territory_id = _DESTINATION_TO_TERRITORY.get(request.destination_id)
        if not territory_id:
            logger.debug(
                "NewTravel: destination_id=%s sin mapeo de territorio",
                request.destination_id,
            )
            return []

        logger.debug(
            "NewTravel: quote start origin=%s destination_id=%s territory_id=%s %s->%s ages=%s",
            request.origin,
            request.destination_id,
            territory_id,
            request.departure_date,
            request.return_date,
            request.ages,
        )

        with httpx.Client(timeout=30.0) as client:
            try:
                token = self._get_token(client, base_url)
                if not token:
                    logger.debug("NewTravel: sin token; no se cotiza")
                    return []
            except httpx.HTTPError:
                logger.exception("NewTravel: error HTTP obteniendo token")
                return []
            except Exception:
                logger.exception("NewTravel: error inesperado obteniendo token")
                return []

            try:
                quoted_plans = self._get_quote_prices(
                    client=client,
                    base_url=base_url,
                    token=token,
                    request=request,
                    territory_id=territory_id,
                )
            except httpx.HTTPError:
                logger.exception("NewTravel: error HTTP en /orders/quote")
                return []
            except Exception:
                logger.exception("NewTravel: error inesperado en /orders/quote")
                return []
            if not quoted_plans:
                logger.debug(
                    "NewTravel: /orders/quote devolvio 0 planes origin=%s destination_id=%s territory_id=%s %s->%s ages=%s",
                    request.origin,
                    request.destination_id,
                    territory_id,
                    request.departure_date,
                    request.return_date,
                    request.ages,
                )
                return []

            plans: list[QuotePlan] = []
            for quoted_plan in quoted_plans:
                try:
                    plan = self._quoted_plan_to_quote_plan(
                        client=client,
                        base_url=base_url,
                        token=token,
                        quoted_plan=quoted_plan,
                    )
                except httpx.HTTPError:
                    logger.exception(
                        "NewTravel: error HTTP armando plan desde quoted_plan idPlan=%s",
                        (quoted_plan or {}).get("idPlan"),
                    )
                    continue
                except Exception:
                    logger.exception(
                        "NewTravel: error inesperado armando plan desde quoted_plan idPlan=%s",
                        (quoted_plan or {}).get("idPlan"),
                    )
                    continue
                if plan:
                    plans.append(plan)
                else:
                    logger.debug(
                        "NewTravel: quoted_plan descartado idPlan=%s name=%s total=%s",
                        (quoted_plan or {}).get("idPlan"),
                        (quoted_plan or {}).get("name"),
                        (quoted_plan or {}).get("total"),
                    )

            logger.debug(
                "NewTravel: quote end ok plans=%s/%s",
                len(plans),
                len(quoted_plans),
            )
            return plans

    def _get_token(self, client: httpx.Client, base_url: str) -> str | None:
        body = {
            "user": self._settings.new_travel_user,
            "password": self._settings.new_travel_password,
        }
        t0 = time.monotonic()
        response = client.post(f"{base_url}/auth/get_token", json=body)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if response.status_code >= 400:
            logger.error(
                "NewTravel: /auth/get_token HTTP %s (%sms) - body=%s",
                response.status_code,
                elapsed_ms,
                response.text,
            )
        else:
            logger.debug("NewTravel: /auth/get_token HTTP %s (%sms)", response.status_code, elapsed_ms)
        response.raise_for_status()
        data = response.json()
        token = (data.get("data") or {}).get("token")
        if not token:
            logger.warning(
                "NewTravel: respuesta sin token keys=%s data_preview=%s",
                sorted(list(data.keys())) if isinstance(data, dict) else type(data).__name__,
                _preview(data),
            )
            return None
        return str(token)

    def _get_quote_prices(
        self,
        client: httpx.Client,
        base_url: str,
        token: str,
        request: QuoteRequest,
        territory_id: str,
    ) -> list[dict[str, Any]]:
        body = {
            "origin": request.origin,
            "destination": territory_id,
            "startDate": request.departure_date.strftime("%Y/%m/%d"),
            "endDate": request.return_date.strftime("%Y/%m/%d"),
            "ages": request.ages,
            # TODO: para planes multiviaje, enviar daysRange (30/45/60/90/120)
            # cuando se definan reglas finales de trip_type -> daysRange.
        }
        headers = {"token": token, "Content-Type": "application/json"}
        t0 = time.monotonic()
        response = client.post(f"{base_url}/orders/quote", headers=headers, json=body)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if response.status_code >= 400:
            logger.error(
                "NewTravel: /orders/quote HTTP %s (%sms) - body=%s request=%s",
                response.status_code,
                elapsed_ms,
                response.text,
                _preview(body),
            )
        else:
            logger.debug(
                "NewTravel: /orders/quote HTTP %s (%sms) request=%s",
                response.status_code,
                elapsed_ms,
                _preview(body),
            )
        response.raise_for_status()
        data = response.json()
        plans = data.get("data") or []
        if not isinstance(plans, list):
            logger.debug(
                "NewTravel: /orders/quote formato inesperado type(data)=%s keys=%s data_preview=%s",
                type(data).__name__,
                sorted(list(data.keys())) if isinstance(data, dict) else None,
                _preview(data),
            )
            return []

        logger.debug("NewTravel: /orders/quote planes=%s", len(plans))
        if len(plans) == 0:
            logger.debug(
                "NewTravel: /orders/quote 200 pero sin planes data_preview=%s",
                _preview(data),
            )
        return plans

    def _quoted_plan_to_quote_plan(
        self,
        client: httpx.Client,
        base_url: str,
        token: str,
        quoted_plan: dict[str, Any],
    ) -> QuotePlan | None:
        plan_id = quoted_plan.get("idPlan")
        if not plan_id:
            return None

        plan_id_str = str(plan_id)
        plan_name = (quoted_plan.get("name") or "").strip() or plan_id_str
        amount = self._parse_price(quoted_plan.get("total"))
        if amount is None:
            return None

        benefits = self._get_benefits_or_empty(
            client=client,
            base_url=base_url,
            token=token,
            plan_id=plan_id_str,
        )
        coverage_amount = self._extract_coverage_from_benefits(benefits)

        return QuotePlan(
            company=self.company_name,
            id=plan_id_str,
            plan_id=plan_id_str,
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=amount,
            final_rate_usd=amount,
            exchange_rate=Decimal("1"),
            final_rate=amount,
        )

    def _parse_price(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        s = str(value).strip().replace(" ", "")
        if not s:
            return None
        # New Travel responde "727.320" (3 decimales). Unificamos a 2 decimales.
        s = s.replace(",", ".")
        try:
            return Decimal(s).quantize(Decimal("0.01"))
        except Exception:
            return None

    def _get_benefits_or_empty(
        self,
        client: httpx.Client,
        base_url: str,
        token: str,
        plan_id: str,
    ) -> list[Benefit]:
        headers = {"token": token}
        try:
            t0 = time.monotonic()
            response = client.get(f"{base_url}/plans/benefits/{plan_id}", headers=headers)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if response.status_code >= 400:
                logger.error(
                    "NewTravel: /plans/benefits/%s HTTP %s (%sms) - body=%s",
                    plan_id,
                    response.status_code,
                    elapsed_ms,
                    response.text,
                )
            else:
                logger.debug(
                    "NewTravel: /plans/benefits/%s HTTP %s (%sms)",
                    plan_id,
                    response.status_code,
                    elapsed_ms,
                )
            response.raise_for_status()
            data = response.json()
        except Exception:
            logger.warning(
                "NewTravel: fallo al obtener beneficios para plan_id=%s; se devuelve sin beneficios",
                plan_id,
                exc_info=True,
            )
            return []

        raw_benefits = data.get("data") or []
        if not isinstance(raw_benefits, list):
            logger.warning(
                "NewTravel: /plans/benefits/%s formato inesperado type(data)=%s keys=%s data_preview=%s",
                plan_id,
                type(data).__name__,
                sorted(list(data.keys())) if isinstance(data, dict) else None,
                _preview(data),
            )
            return []

        benefits: list[Benefit] = []
        for item in raw_benefits:
            if not isinstance(item, dict):
                continue
            benefit_id = item.get("idBenefit")
            if benefit_id is None:
                continue
            nombre = str(item.get("benefit") or "").strip()
            valor = str(item.get("coverage") or "").strip()
            try:
                benefit_id_int = int(benefit_id)
            except Exception:
                continue
            benefits.append(
                Benefit(
                    id=benefit_id_int,
                    nombre=nombre,
                    valor=valor,
                )
            )
        return benefits

    def _extract_coverage_from_benefits(self, benefits: list[Benefit]) -> Decimal:
        # Regla New Travel: el "monto global" (máximo) viene en el primer beneficio.
        if not benefits:
            return Decimal("0")
        first_amount = self._parse_usd_coverage(benefits[0].valor)
        if first_amount is None:
            return Decimal("0")
        return first_amount

        return Decimal("0")

    def _parse_coverage_value(self, value: str) -> Decimal:
        if not value:
            return Decimal("0")
        s = value.upper().replace("USD", "").strip()
        s = s.replace(".", "").replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return Decimal("0")

    def _parse_usd_coverage(self, raw_value: str) -> Decimal | None:
        if not raw_value:
            return None
        upper_value = raw_value.upper()
        # New Travel puede venir en USD o EUR (incluyendo símbolo €).
        if (
            "USD" not in upper_value
            and "U$S" not in upper_value
            and "EUR" not in upper_value
            and "€" not in raw_value
        ):
            return None
        match = re.search(r"(\d[\d\.,]*)", raw_value)
        if not match:
            return None
        digits_only = re.sub(r"\D", "", match.group(1))
        if not digits_only:
            return None
        try:
            return Decimal(digits_only)
        except Exception:
            return None


def _preview(value: Any, max_len: int = 800) -> str:
    """
    Preview segura para logs (sin credenciales).
    - Trunca payloads grandes para evitar spam en logs.
    - No intenta serializar objetos complejos (usa str()).
    """
    try:
        s = str(value)
    except Exception:
        return "<unprintable>"
    s = s.replace("\n", "\\n")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...<truncated>"
