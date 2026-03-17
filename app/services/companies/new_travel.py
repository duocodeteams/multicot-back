"""Adaptador para New Travel (cotización por /orders/quote + beneficios por plan)."""

import logging
from decimal import Decimal
from typing import Any

import httpx

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

        with httpx.Client(timeout=30.0) as client:
            token = self._get_token(client, base_url)
            if not token:
                return []

            quoted_plans = self._get_quote_prices(
                client=client,
                base_url=base_url,
                token=token,
                request=request,
                territory_id=territory_id,
            )
            if not quoted_plans:
                return []

            plans: list[QuotePlan] = []
            for quoted_plan in quoted_plans:
                plan = self._quoted_plan_to_quote_plan(
                    client=client,
                    base_url=base_url,
                    token=token,
                    quoted_plan=quoted_plan,
                )
                if plan:
                    plans.append(plan)
            return plans

    def _get_token(self, client: httpx.Client, base_url: str) -> str | None:
        body = {
            "user": self._settings.new_travel_user,
            "password": self._settings.new_travel_password,
        }
        response = client.post(f"{base_url}/auth/get_token", json=body)
        if response.status_code >= 400:
            logger.error(
                "NewTravel: /auth/get_token HTTP %s - body=%s",
                response.status_code,
                response.text,
            )
        response.raise_for_status()
        data = response.json()
        token = (data.get("data") or {}).get("token")
        if not token:
            logger.warning("NewTravel: respuesta sin token")
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
        response = client.post(f"{base_url}/orders/quote", headers=headers, json=body)
        if response.status_code >= 400:
            logger.error(
                "NewTravel: /orders/quote HTTP %s - body=%s",
                response.status_code,
                response.text,
            )
        response.raise_for_status()
        data = response.json()
        plans = data.get("data") or []
        return plans if isinstance(plans, list) else []

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
            response = client.get(f"{base_url}/plans/benefits/{plan_id}", headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "NewTravel: /plans/benefits/%s HTTP %s - body=%s",
                    plan_id,
                    response.status_code,
                    response.text,
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
        for benefit in benefits:
            if benefit.nombre.strip().lower() == "monto global":
                return self._parse_coverage_value(benefit.valor)
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
