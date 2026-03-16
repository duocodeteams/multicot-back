"""Adaptador para Pax Assistance.

Por ahora solo implementa cotización (quoter). La emisión usaría:
    - searchId devuelto por /api/assistance/external/quoter
    - plan.id del producto seleccionado
en el endpoint /api/assistance/external/emitter.
"""

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
    TRIP_TYPE_LARGA_ESTADIA,
    TRIP_TYPE_MULTIVIAJE,
    TRIP_TYPE_UNICO_VIAJE,
    Benefit,
    QuotePlan,
    QuoteRequest,
)

logger = logging.getLogger(__name__)


# destination_id (1-5) → destination[] de Pax
_DESTINATION_MAP: dict[int, list[str]] = {
    DESTINO_ID_NACIONAL: ["OWN_COUNTRY"],
    DESTINO_ID_LATINOAMERICA: ["SOUTH_AMERICA"],
    DESTINO_ID_EUROPA: ["EUROPE"],
    DESTINO_ID_RESTO_MUNDO: ["MULTI"],
    DESTINO_ID_NORTEAMERICA: ["NORTH_AMERICA"],
}

# trip_type → tripType de Pax
_TRIP_TYPE_MAP: dict[str, str] = {
    TRIP_TYPE_UNICO_VIAJE: "ONE_TRIP",
    TRIP_TYPE_MULTIVIAJE: "MULTI_TRIP",
    TRIP_TYPE_LARGA_ESTADIA: "LONG_STAY",
}


class PaxQuoteProvider:
    company_name = "Pax"

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.pax_api_key:
            logger.debug("Pax: sin PAX_API_KEY configurado")
            return []

        base_url = settings.pax_base_url.rstrip("/")

        # Mapeo destino (destination_id → destination[])
        destinations = _DESTINATION_MAP.get(request.destination_id)
        if not destinations:
            logger.debug(
                "Pax: destination_id=%s sin mapeo a destino Pax",
                request.destination_id,
            )
            return []

        # Mapeo tipo de viaje (trip_type → tripType)
        pax_trip_type = _TRIP_TYPE_MAP.get(request.trip_type)
        if not pax_trip_type:
            logger.debug("Pax: trip_type='%s' sin mapeo a tripType Pax", request.trip_type)
            return []

        # Lógica de edades:
        #   - 0 a 74 años → adults
        #   - 75 a 84 años → elder
        #   - 85+ → sin planes (no se cotiza)
        adults = 0
        elders = 0
        for age in request.ages:
            if age >= 85:
                logger.debug("Pax: edad %s >= 85, no se generan planes", age)
                return []
            if 75 <= age <= 84:
                elders += 1
            else:
                adults += 1

        # Pax indica que elder es mutuamente excluyente con adults/infants/children.
        # Si hay al menos un elder y también adults, por ahora preferimos no cotizar
        # para evitar violar la restricción.
        if elders > 0 and adults > 0:
            logger.debug(
                "Pax: combinación de adultos (%s) y seniors (%s) no soportada",
                adults,
                elders,
            )
            return []

        # Armamos params respetando la validación de Pax:
        # - Si un campo (adults/infants/children/elder) va presente, debe ser >= 1.
        # - Si no aplica, es mejor no enviarlo (no mandar "0").
        params: dict[str, Any] = {
            "tripType": pax_trip_type,
            "startDate": request.departure_date.isoformat(),
            "endDate": request.return_date.isoformat(),
            "countryCode": request.origin,
        }
        if elders > 0:
            params["elder"] = elders
        else:
            if adults > 0:
                params["adults"] = adults
        # destination[] puede repetirse; httpx envía listas como parámetros repetidos.
        params["destination[]"] = destinations

        headers = {
            "x-api-key": settings.pax_api_key,
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=30.0) as client:
            logger.debug("Pax: GET /api/assistance/external/quoter params=%s", params)
            response = client.get(
                f"{base_url}/api/assistance/external/quoter",
                params=params,
                headers=headers,
            )
            if response.status_code >= 400:
                logger.error(
                    "Pax: /quoter HTTP %s - body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            data = response.json()

        search_id = data.get("searchId", "")
        productos = data.get("data") or []
        if not productos:
            logger.info("Pax: cotización OK pero sin productos para params=%s", params)
            return []

        plans: list[QuotePlan] = []
        for prod in productos:
            plan = self._producto_to_quote_plan(prod, search_id)
            if plan:
                plans.append(plan)
        return plans

    def _producto_to_quote_plan(
        self,
        producto: dict[str, Any],
        search_id: str,
    ) -> QuotePlan | None:
        plan_id = producto.get("id")
        if not plan_id:
            return None

        price = producto.get("price")
        if price is None:
            return None
        try:
            amount = Decimal(str(price)).quantize(Decimal("0.01"))
        except Exception:
            return None

        ccgg = producto.get("ccgg") or []
        coverage_amount = self._extract_coverage_from_ccgg(ccgg)
        benefits = self._build_benefits_from_ccgg(ccgg)

        plan_name = (producto.get("plan") or "").strip() or str(plan_id)
        plan_id_str = str(plan_id)
        # Guardamos searchId en el id compuesto, para posible uso futuro en emisión.
        quote_id = f"{search_id}|{plan_id_str}" if search_id else plan_id_str

        return QuotePlan(
            company=self.company_name,
            id=quote_id,
            plan_id=plan_id_str,
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=amount,
            final_rate_usd=amount,
            exchange_rate=Decimal("1"),
            final_rate=amount,
        )

    def _extract_coverage_from_ccgg(self, ccgg: list[dict[str, Any]]) -> Decimal:
        """Toma el value de la primera prestación como monto de cobertura."""
        if not ccgg:
            return Decimal("0")
        first = ccgg[0]
        valor = (first.get("value") or "").strip()
        if not valor:
            return Decimal("0")
        return self._parse_coverage_valor(valor)

    def _parse_coverage_valor(self, valor_str: str) -> Decimal:
        """Parsea strings tipo 'USD/EUR 30.001' a Decimal (30001)."""
        if not valor_str:
            return Decimal("0")
        s = valor_str.upper()
        # Eliminar prefijos de moneda comunes.
        for prefix in ("USD/EUR", "EUR/USD", "USD", "EUR", "U$D", "U$S", "USS"):
            s = s.replace(prefix, "")
        s = s.strip()
        # Quitar espacios intermedios, usar '.' como separador decimal.
        s = s.replace(" ", "")
        # Primero reemplazamos puntos de miles y comas decimales.
        s = s.replace(".", "").replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return Decimal("0")

    def _build_benefits_from_ccgg(self, ccgg: list[dict[str, Any]]) -> list[Benefit]:
        benefits: list[Benefit] = []
        for idx, item in enumerate(ccgg):
            if not isinstance(item, dict):
                continue
            nombre = str(item.get("description", "")).strip()
            valor = str(item.get("value", "")).strip()
            benefits.append(
                Benefit(
                    id=idx,
                    nombre=nombre,
                    valor=valor,
                )
            )
        return benefits
