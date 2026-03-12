"""
Adaptador para Cardinal Assistance (Evoucher 2).
Documentación: https://evoucher.cardinalassistance.com/docs/doc.html

IDs de moneda Cardinal (en costoLista/costoFinal.currency):
    Por documentar: obtener de POST /parametros con parametros=monedas.
    Ejemplo observado: 3 = USD, 7 = ARS (pesos).
"""
import logging
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from app.core.config import get_cardinal_destino_ids, get_settings
from app.quotations.schemas import Benefit, QuotePlan, QuoteRequest, TRIP_TYPE_MULTIVIAJE

# Origen del cotizador → nombre en Cardinal para resolver vía /parametros.
ORIGEN_AR_NOMBRE_CARDINAL = "Argentina"
# Para productos anuales (multiviaje): días mínimos que debe cubrir el producto.
MAX_DIAS_CORRIDOS_ANUAL = 30


class CardinalQuoteProvider:
    company_name = "Cardinal"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._destino_ids = get_cardinal_destino_ids(self._settings)

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        if not self._settings.cardinal_agente_emisor_guid:
            logger.debug("Cardinal: sin CARDINAL_AGENTE_EMISOR_GUID configurado")
            return []

        destino_id = self._destino_ids.get(request.destination_id)
        if destino_id is None:
            logger.debug(
                "Cardinal: destination_id=%s sin mapeo a Cardinal destinoId en .env",
                request.destination_id,
            )
            return []

        origen_id = self._resolve_origen_id(request.origin)
        if origen_id is None:
            logger.warning(
                "Cardinal: no se pudo resolver origen '%s' a Cardinal origenId",
                request.origin,
            )
            return []

        tarifa_anual = request.trip_type == TRIP_TYPE_MULTIVIAJE
        body: dict[str, Any] = {
            "agenteEmisorGuid": self._settings.cardinal_agente_emisor_guid,
            "origenId": origen_id,
            "destinoId": destino_id,
            "fechaSalida": request.departure_date.isoformat(),
            "fechaRegreso": request.return_date.isoformat(),
            "edades": request.ages,
            "tarifaAnual": tarifa_anual,
            "email": self._settings.cotizador_default_email,
            "localeId": 1,
            "prestaciones": "cotizador",
        }
        if tarifa_anual:
            body["maxDiasCorridos"] = MAX_DIAS_CORRIDOS_ANUAL

        base_url = self._settings.cardinal_base_url.rstrip("/")
        with httpx.Client(timeout=30.0) as client:
            logger.debug("Cardinal: POST /cotizar body=%s", body)
            response = client.post(f"{base_url}/cotizar", json=body)
            if response.status_code >= 400:
                logger.error(
                    "Cardinal: /cotizar HTTP %s - body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            data = response.json()

        if "cotizacion" not in data or "productos" not in data["cotizacion"]:
            logger.warning(
                "Cardinal: respuesta sin cotizacion/productos: %s",
                {k: v for k, v in data.items() if k != "productos"},
            )
            return []

        # guid: necesario para /emitir cuando implementemos emisión.
        cotizacion_guid = data["cotizacion"].get("guid", "")
        productos = data["cotizacion"]["productos"]
        if not productos:
            logger.info(
                "Cardinal: cotización OK pero productos vacío (tarifaAnual=%s). "
                "Puede que no haya planes para esta combinación origen/destino/tipo.",
                tarifa_anual,
            )
        plans: list[QuotePlan] = []
        for prod in productos:
            plan = self._producto_to_quote_plan(prod, cotizacion_guid)
            if plan:
                plans.append(plan)
        return plans

    def _resolve_origen_id(self, origin: str) -> int | None:
        """Obtiene origenId de Cardinal vía /parametros. Por ahora solo 'AR' → Argentina."""
        if origin != "AR":
            return None
        base_url = self._settings.cardinal_base_url.rstrip("/")
        with httpx.Client(timeout=15.0) as client:
            body = {
                "agenteEmisorGuid": self._settings.cardinal_agente_emisor_guid,
                "parametros": "origenes",
                "localeId": 1,
            }
            response = client.post(f"{base_url}/parametros", json=body)
            if response.status_code >= 400:
                logger.error(
                    "Cardinal: /parametros HTTP %s - body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            data = response.json()
        origenes = data.get("origenes") or []
        for o in origenes:
            if isinstance(o, dict) and (o.get("nombre") or "").strip() == ORIGEN_AR_NOMBRE_CARDINAL:
                id_val = o.get("id")
                if id_val is not None:
                    return int(id_val)
        return None

    # Comisión para calcular net_rate (final_rate_usd - comisión). Por ahora 50%.
    _COMISION_PORCENTAJE = Decimal("0.50")

    def _producto_to_quote_plan(
        self,
        producto: dict[str, Any],
        cotizacion_guid: str,
    ) -> QuotePlan | None:
        producto_id = producto.get("productoId")
        if producto_id is None:
            return None

        costo_final = producto.get("costoFinal") or {}
        amount_final = self._parse_amount(costo_final)
        if amount_final is None:
            return None

        prestaciones = producto.get("prestaciones") or []
        coverage_amount = self._extract_coverage_from_prestaciones(prestaciones)
        benefits = [
            Benefit(
                id=p.get("prestacionId", 0),
                nombre=str(p.get("nombre", "")),
                valor=str(p.get("valor", "")),
            )
            for p in prestaciones
            if isinstance(p, dict)
        ]

        # net_rate = final_rate_usd menos comisión (50% por ahora)
        net_rate = (
            amount_final * (Decimal("1") - self._COMISION_PORCENTAJE)
        ).quantize(Decimal("0.01"))

        plan_id_str = str(producto_id)
        quote_id = f"{cotizacion_guid}|{plan_id_str}"
        plan_name = (producto.get("productoNombre") or "").strip() or plan_id_str

        return QuotePlan(
            company=self.company_name,
            id=quote_id,
            plan_id=plan_id_str,
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=net_rate,
            final_rate_usd=amount_final,
            exchange_rate=Decimal("1"),
            final_rate=amount_final,
        )

    def _extract_coverage_from_prestaciones(
        self, prestaciones: list[dict[str, Any]]
    ) -> Decimal:
        """Extrae el Tope Máximo Global (coverage) de la primera prestación que lo indique."""
        for p in prestaciones:
            if not isinstance(p, dict):
                continue
            nombre = (p.get("nombre") or "").strip()
            if "Tope Máximo Global" in nombre:
                valor_str = (p.get("valor") or "").strip()
                return self._parse_coverage_valor(valor_str)
        return Decimal("0")

    def _parse_coverage_valor(self, valor_str: str) -> Decimal:
        """Parsea 'USS 50.000 ' o 'USS 150.000' a Decimal (50000, 150000)."""
        if not valor_str:
            return Decimal("0")
        s = valor_str.upper().replace("USS", "").strip()
        s = s.replace(".", "").replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return Decimal("0")

    def _parse_amount(self, cost_obj: dict[str, Any]) -> Decimal | None:
        amount = cost_obj.get("amount")
        if amount is None:
            return None
        try:
            return Decimal(str(amount))
        except Exception:
            return None
