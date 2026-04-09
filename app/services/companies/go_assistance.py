"""
Adaptador para GoAssistance (ATV - Asegura Tu Viaje).
API: POST para cotizar (devuelve Token; Productos suelen venir vacíos),
GET con token para obtener los productos.

Precios: Costo = USD (MonedaISO); CostoOrigen = moneda del país de origen (ej. ARS si PaisDesde=AR).
"""
import logging
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings
from app.quotations.schemas import Benefit, QuotePlan, QuoteRequest

logger = logging.getLogger(__name__)

# destination_id (1-5) → PaisHasta GoAssistance
# 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica
DESTINO_MAP: dict[int, int] = {
    1: 1009,  # Cobertura Nacional
    2: 1002,  # América del Sur
    3: 1004,  # Europa y Mediterráneo
    4: 1006,  # Resto del Mundo
    5: 1000,  # América del Norte
}
PAIS_DESDE_AR = 32  # Argentina
TIPO_VIAJE_PLACER = 1  # Por ahora siempre Placer


class GoAssistanceQuoteProvider:
    company_name = "GoAssistance"

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        if not self._settings.go_assistance_webservice:
            logger.debug("GoAssistance: sin GO_ASSISTANCE_WEBSERVICE configurado")
            return []

        if request.origin != "AR":
            logger.debug("GoAssistance: origen '%s' no soportado (solo AR)", request.origin)
            return []

        pais_hasta = DESTINO_MAP.get(request.destination_id)
        if pais_hasta is None:
            logger.debug(
                "GoAssistance: destination_id=%s sin mapeo",
                request.destination_id,
            )
            return []

        base_url = self._settings.go_assistance_base_url.rstrip("/")
        edades = self._edades_to_edad1_5(request.ages)

        post_body = {
            "PaisDesde": PAIS_DESDE_AR,
            "PaisHasta": pais_hasta,
            "TipoViaje": TIPO_VIAJE_PLACER,
            "FechaDesde": request.departure_date.isoformat(),
            "FechaHasta": request.return_date.isoformat(),
            "Edad1": edades[0],
            "Edad2": edades[1],
            "Edad3": edades[2],
            "Edad4": edades[3],
            "Edad5": edades[4],
            "Cultura": "es-ES",
            "Email": self._settings.cotizador_default_email,
            "WebService": self._settings.go_assistance_webservice,
            "SemanaGestacion": 0,
            "Source": "",
            "Token": "",
            "Telefono": "",
        }

        with httpx.Client(timeout=30.0) as client:
            logger.debug("GoAssistance: POST /api/CotizacionSeguroViajero")
            response = client.post(
                f"{base_url}/api/CotizacionSeguroViajero",
                json=post_body,
            )
            if response.status_code >= 400:
                logger.error(
                    "GoAssistance: POST HTTP %s - body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            post_data = response.json()

            token = post_data.get("Token")
            if not token:
                logger.warning("GoAssistance: respuesta sin Token")
                return []

            # GET con token: Productos vienen en la respuesta del GET (POST suele devolver Productos vacíos)
            logger.debug("GoAssistance: GET /api/CotizacionSeguroViajero?token=...")
            get_response = client.get(
                f"{base_url}/api/CotizacionSeguroViajero",
                params={"token": token, "idseguro": 0},
            )
            if get_response.status_code >= 400:
                logger.error(
                    "GoAssistance: GET HTTP %s - body=%s",
                    get_response.status_code,
                    get_response.text,
                )
            get_response.raise_for_status()
            get_data = get_response.json()

            productos = get_data.get("Productos") or []
        plans: list[QuotePlan] = []
        for prod in productos:
            plan = self._producto_to_quote_plan(prod, token)
            if plan:
                plans.append(plan)
        return plans

    def _edades_to_edad1_5(self, ages: list[int]) -> tuple[int, int, int, int, int]:
        """Mapea ages a Edad1..Edad5. Rellena con 0 si hay menos de 5."""
        result = [0, 0, 0, 0, 0]
        for i, age in enumerate(ages[:5]):
            result[i] = age
        return tuple(result)

    def _producto_to_quote_plan(
        self, producto: dict[str, Any], cotizacion_token: str
    ) -> QuotePlan | None:
        producto_id = producto.get("ProductoId")
        if producto_id is None:
            return None

        costo = producto.get("Costo")
        if costo is None:
            return None
        try:
            amount_usd = Decimal(str(costo)).quantize(Decimal("0.01"))
        except Exception:
            return None

        # Precio en moneda de origen (ARS para PaisDesde Argentina). Misma moneda que final_rate.
        amount_origen = self._parse_optional_decimal(producto.get("CostoOrigen"))
        if amount_origen is not None:
            final_rate = amount_origen
            if amount_usd > 0:
                exchange_rate = (amount_origen / amount_usd).quantize(Decimal("0.0001"))
            else:
                exchange_rate = Decimal("1")
        else:
            final_rate = amount_usd
            exchange_rate = Decimal("1")

        coverage_amount = self._parse_cobertura_enfermedad(
            producto.get("CoberturaEnfermedad")
        )
        benefits = self._build_benefits(producto)
        plan_name = (producto.get("Producto") or "").strip() or str(producto_id)
        plan_id_str = str(producto_id)
        quote_id = f"{cotizacion_token}|{plan_id_str}"

        return QuotePlan(
            company=self.company_name,
            id=quote_id,
            plan_id=plan_id_str,
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=final_rate,
            final_rate_usd=amount_usd,
            exchange_rate=exchange_rate,
            final_rate=final_rate,
        )

    def _parse_optional_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except Exception:
            return None

    def _parse_cobertura_enfermedad(self, valor: Any) -> Decimal:
        """Parsea 'U$D 3.000', '€/U$D 45.000', 'USD 60.000' etc. a Decimal (3000, 45000, 60000)."""
        if valor is None or not str(valor).strip():
            return Decimal("0")
        s = str(valor).upper().replace("\t", " ").strip()
        for prefix in ("€/U$D", "€/USD", "U$D", "USD", "€"):
            s = s.replace(prefix, "")
        s = s.strip().replace(".", "").replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return Decimal("0")

    def _normalize_benefit_valor(self, valor: Any) -> str:
        """Normaliza valor de cobertura a formato 'USD X.XXX' para consistencia."""
        if valor is None or not str(valor).strip():
            return ""
        d = self._parse_cobertura_enfermedad(valor)
        if d == 0:
            return str(valor).strip()
        n = int(d) if d == int(d) else float(d)
        if n >= 1000:
            formatted = f"{int(n):,}".replace(",", ".")
        else:
            formatted = str(int(n)) if n == int(n) else str(n)
        return f"USD {formatted}"

    def _build_benefits(self, producto: dict[str, Any]) -> list[Benefit]:
        """Construye benefits desde campos de cobertura de GoAssistance."""
        benefits: list[Benefit] = []
        idx = 0

        cobertura_enfermedad = producto.get("CoberturaEnfermedad")
        if cobertura_enfermedad and str(cobertura_enfermedad).strip():
            benefits.append(
                Benefit(
                    id=idx,
                    nombre="Cobertura Enfermedad",
                    valor=self._normalize_benefit_valor(cobertura_enfermedad),
                )
            )
            idx += 1

        cobertura_enfermedad_preexistente = producto.get("CoberturaEnfermedadPreexistente")
        if cobertura_enfermedad_preexistente and str(cobertura_enfermedad_preexistente).strip():
            benefits.append(
                Benefit(
                    id=idx,
                    nombre="Cobertura Enfermedad Preexistente",
                    valor=self._normalize_benefit_valor(cobertura_enfermedad_preexistente),
                )
            )
            idx += 1

        cobertura_accidente = producto.get("CoberturaAccidente")
        if cobertura_accidente and str(cobertura_accidente).strip():
            benefits.append(
                Benefit(
                    id=idx,
                    nombre="Cobertura Accidente",
                    valor=self._normalize_benefit_valor(cobertura_accidente),
                )
            )
            idx += 1

        cobertura_equipaje = producto.get("CoberturaEquipaje")
        if cobertura_equipaje and str(cobertura_equipaje).strip():
            benefits.append(
                Benefit(
                    id=idx,
                    nombre="Cobertura Equipaje",
                    valor=self._normalize_benefit_valor(cobertura_equipaje),
                )
            )
            idx += 1

        cobertura_cancelacion = producto.get("CoberturaCancelacionViaje")
        if cobertura_cancelacion and str(cobertura_cancelacion).strip():
            benefits.append(
                Benefit(
                    id=idx,
                    nombre="Cobertura Cancelación Viaje",
                    valor=self._normalize_benefit_valor(cobertura_cancelacion),
                )
            )

        return benefits
