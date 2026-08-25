"""
Adaptador para Inter Assist.

La API no expone un endpoint de cotización. Se cotiza localmente a partir del
catálogo GET /api/planes (tarifas por tramo de días / edad / tipo de viaje).

Tarifa diaria (unico_viaje):
- Edades: 0-69 (sin sufijo), 70-85 (_70), 86+ (_86).
- Bloques de días: los que traiga cada plan (_5_dias, _10_dias, ...).
- Si days cae en un bloque → tarifa del bloque.
- Si está entre bloques → bloque anterior + (días extra) * dia_adicional.
- Si supera el último → último + días extra * dia_adicional.

Multiviaje: tarifa anual fija (_30_dias_anual por default), igual que
Cardinal maxDiasCorridos / Omint quantityOfDays. No usa la duración del viaje.

IMPORTANTE: no usar POST /api/ventas para cotizar (emite vouchers).
"""
from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import get_settings
from app.quotations.schemas import (
    DESTINO_ID_EUROPA,
    DESTINO_ID_LATINOAMERICA,
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

_DESTINATION_TO_INTERASSIST: dict[int, int] = {
    DESTINO_ID_LATINOAMERICA: 2,
    DESTINO_ID_EUROPA: 3,
    DESTINO_ID_RESTO_MUNDO: 1,
    DESTINO_ID_NORTEAMERICA: 1,
}

# Fallback si un plan no expone keys de días parseables.
_DAILY_BRACKETS_DEFAULT: tuple[int, ...] = (5, 10, 16, 22, 30, 45, 60, 90)
_MAX_PAGES = 50

# Keys de tarifa diaria por edad: 0-69 sin sufijo, 70-85 → _70, 86+ → _86
_DAILY_KEY_RE_BASE = re.compile(r"^_(\d+)_dias$")
_DAILY_KEY_RE_70 = re.compile(r"^_(\d+)_dias_70$")
_DAILY_KEY_RE_86 = re.compile(r"^_(\d+)_dias_86$")


class InterAssistQuoteProvider:
    company_name = "Inter Assist"

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.interassist_api_key:
            return []

        destino_id = _DESTINATION_TO_INTERASSIST.get(request.destination_id)
        if destino_id is None:
            return []

        if request.origin != "AR":
            return []

        trip_days = (request.return_date - request.departure_date).days + 1
        if trip_days < 1:
            return []

        base_url = settings.interassist_base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {settings.interassist_api_key}",
            "Accept": "application/json",
        }

        with httpx.Client(timeout=30.0) as client:
            try:
                raw_plans = self._fetch_all_plans(client, base_url, headers)
            except httpx.HTTPError:
                logger.exception("InterAssist: error HTTP obteniendo planes")
                return []
            except Exception:
                logger.exception("InterAssist: error inesperado obteniendo planes")
                return []

        pais_id = settings.interassist_pais_argentina_id
        print(
            f"[InterAssist RAW] request trip_type={request.trip_type} "
            f"days={trip_days} ages={request.ages} destino_id={destino_id}"
        )
        plans: list[QuotePlan] = []
        for raw in raw_plans:
            try:
                plan = self._plan_to_quote_plan(
                    raw=raw,
                    request=request,
                    destino_id=destino_id,
                    pais_id=pais_id,
                    trip_days=trip_days,
                )
            except Exception as e:
                print(f"[InterAssist RAW] EXCEPCION plan id={raw.get('id')}: {e}")
                continue
            if plan:
                self._debug_print_raw_tarifas(raw, request, trip_days, plan)
                plans.append(plan)

        print(f"[InterAssist RAW] planes cotizados={len(plans)} de {len(raw_plans)}")
        return plans

    def _debug_print_raw_tarifas(
        self,
        raw: dict[str, Any],
        request: QuoteRequest,
        trip_days: int,
        plan: QuotePlan,
    ) -> None:
        """Dump de tarifas crudas de la API vs precio calculado (solo debug)."""
        plan_id = raw.get("id")
        nombre = raw.get("nombre")
        moneda = (raw.get("moneda") or {}).get("nombre") if isinstance(raw.get("moneda"), dict) else raw.get("moneda")
        lcc = raw.get("local_currency_conversion")
        descuento = raw.get("descuento")
        tipo_emision = raw.get("tipo_emision")
        tipo_tarifa = raw.get("tipo_tarifa")

        # Campos de tarifa crudos (diarios / anuales / long stay / dia adicional)
        tarifa_keys = [
            k for k in raw.keys()
            if (
                k.startswith("_")
                or k.startswith("dia_adicional")
            )
            and raw.get(k) not in (None, "", "0", "0.00", 0, 0.0)
        ]
        tarifas = {k: raw.get(k) for k in sorted(tarifa_keys)}

        # Qué fórmula usamos por pasajero
        por_pasajero = []
        for age in request.ages:
            suffix = self._age_suffix(age)
            formula = None
            price = None
            if request.trip_type == TRIP_TYPE_UNICO_VIAJE:
                formula = self._daily_price_formula(raw, trip_days, suffix)
                price = self._daily_price(raw, trip_days, suffix)
            elif request.trip_type == TRIP_TYPE_MULTIVIAJE:
                annual_days = self._settings.interassist_annual_days
                formula = (
                    f"_{annual_days}_dias{suffix}_anual"
                    if suffix
                    else f"_{annual_days}_dias_anual"
                )
                price = self._annual_price(raw, trip_days, suffix)
            elif request.trip_type == TRIP_TYPE_LARGA_ESTADIA:
                months = max(3, math.ceil(trip_days / 30))
                formula = f"_{min(months, 15)}_meses{suffix}"
                price = self._long_stay_price(raw, trip_days, suffix)
            por_pasajero.append(
                {
                    "age": age,
                    "age_band": "86+" if suffix == "_86" else ("70-85" if suffix == "_70" else "0-69"),
                    "formula": formula,
                    "price_used": str(price) if price is not None else None,
                }
            )

        print(
            f"[InterAssist RAW] plan id={plan_id} nombre='{nombre}' "
            f"moneda={moneda} lcc={lcc} descuento={descuento} "
            f"tipo_tarifa={tipo_tarifa} tipo_emision={tipo_emision}"
        )
        print(f"[InterAssist RAW]   cobertura={raw.get('cobertura')} "
              f"edad_maxima={raw.get('edad_maxima')} activo={raw.get('activo')}")
        print(f"[InterAssist RAW]   tarifas_no_cero={tarifas}")
        print(f"[InterAssist RAW]   calculo_por_pasajero={por_pasajero}")
        print(
            f"[InterAssist RAW]   RESULTADO final_rate_usd={plan.final_rate_usd} "
            f"final_rate={plan.final_rate} exchange_rate={plan.exchange_rate} "
            f"net_rate={plan.net_rate}"
        )

    def _fetch_all_plans(
        self,
        client: httpx.Client,
        base_url: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        all_plans: list[dict[str, Any]] = []
        page = 1
        last_page: int | None = None

        while page <= _MAX_PAGES:
            url = f"{base_url}/api/planes"
            response = client.get(
                url,
                headers=headers,
                params={"page": page},
            )
            if response.status_code >= 400:
                logger.error(
                    "InterAssist: GET /api/planes page=%s HTTP %s - body=%s",
                    page,
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            payload = response.json()

            page_items, detected_last = self._extract_page(payload)
            if not page_items:
                break

            all_plans.extend(page_items)
            if detected_last is not None:
                last_page = detected_last
            if last_page is not None and page >= last_page:
                break
            if detected_last is None and len(page_items) == 0:
                break
            # Si no hay meta de paginación, avanzar solo si vino una página "llena".
            # Sin last_page, cortamos cuando la página viene vacía (arriba) o tras una sola página corta.
            if last_page is None and page == 1 and len(page_items) < 10:
                # Heurística: respuestas sin paginación o primera página corta.
                # Seguimos a page 2; si viene vacía, cortamos.
                pass
            page += 1

        logger.debug("InterAssist: planes obtenidos=%s pages_fetched=%s", len(all_plans), page - 1)
        return all_plans

    def _extract_page(
        self, payload: Any
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Soporta list directa o paginación tipo Laravel ({data, meta/last_page})."""
        if isinstance(payload, list):
            items = [p for p in payload if isinstance(p, dict)]
            return items, 1

        if not isinstance(payload, dict):
            return [], 1

        data = payload.get("data")
        if isinstance(data, list):
            items = [p for p in data if isinstance(p, dict)]
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            items = [p for p in data["data"] if isinstance(p, dict)]
        else:
            items = []

        last_page = None
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        for key in ("last_page", "lastPage"):
            raw = payload.get(key, meta.get(key))
            if raw is not None:
                try:
                    last_page = int(raw)
                    break
                except (TypeError, ValueError):
                    pass
        return items, last_page

    def _plan_to_quote_plan(
        self,
        raw: dict[str, Any],
        request: QuoteRequest,
        destino_id: int,
        pais_id: int,
        trip_days: int,
    ) -> QuotePlan | None:
        if not raw.get("activo", True):
            return None

        plan_id = raw.get("id")
        if plan_id is None:
            return None

        if not self._matches_destino(raw, destino_id):
            return None
        if not self._matches_pais(raw, pais_id):
            return None

        edad_maxima = raw.get("edad_maxima")
        if edad_maxima is not None:
            try:
                max_age = int(edad_maxima)
            except (TypeError, ValueError):
                max_age = None
            if max_age is not None and any(age > max_age for age in request.ages):
                return None

        total = self._calculate_total_price(raw, request, trip_days)
        if total is None or total <= 0:
            return None

        plan_name = str(raw.get("nombre") or "").strip() or str(plan_id)
        benefits = self._extract_benefits(raw)
        coverage_amount = self._extract_coverage(raw, benefits)
        final_rate_usd = total.quantize(Decimal("0.01"))

        # La tarifa del plan está en la moneda indicada por "moneda" (ej. Dólar).
        # local_currency_conversion funciona como TC a moneda local cuando > 1.
        # Patrón idéntico a Cardinal/GoAssistance/Terrawind:
        #   - Si hay TC válido → final_rate = USD * TC (moneda local), exchange_rate = TC
        #   - Si no hay TC     → final_rate = USD, exchange_rate = 1
        lcc = self._parse_decimal(raw.get("local_currency_conversion"))
        if lcc is not None and lcc > 1:
            exchange_rate = lcc.quantize(Decimal("0.0001"))
            final_rate = (final_rate_usd * exchange_rate).quantize(Decimal("0.01"))
            net_rate = final_rate
        else:
            exchange_rate = Decimal("1")
            final_rate = final_rate_usd
            net_rate = final_rate_usd

        return QuotePlan(
            company=self.company_name,
            id=str(plan_id),
            plan_id=str(plan_id),
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=net_rate,
            final_rate_usd=final_rate_usd,
            exchange_rate=exchange_rate,
            final_rate=final_rate,
        )

    def _matches_destino(self, raw: dict[str, Any], destino_id: int) -> bool:
        destinos = raw.get("destinos") or []
        if not isinstance(destinos, list) or not destinos:
            return False
        for dest in destinos:
            if isinstance(dest, dict) and dest.get("id") == destino_id:
                return True
            if dest == destino_id:
                return True
        return False

    def _matches_pais(self, raw: dict[str, Any], pais_id: int) -> bool:
        paises = raw.get("paises") or []
        if not isinstance(paises, list) or not paises:
            # Sin filtro de país en el plan: lo aceptamos.
            return True
        for pais in paises:
            if isinstance(pais, dict) and pais.get("id") == pais_id:
                return True
            if pais == pais_id:
                return True
        return False

    def _calculate_total_price(
        self,
        raw: dict[str, Any],
        request: QuoteRequest,
        trip_days: int,
    ) -> Decimal | None:
        total = Decimal("0")
        for age in request.ages:
            price = self._price_for_passenger(raw, request.trip_type, trip_days, age)
            if price is None:
                return None
            total += price

        descuento = self._parse_decimal(raw.get("descuento")) or Decimal("0")
        if descuento > 0:
            # Se interpreta como porcentaje (ej. 10 = 10%).
            total = total * (Decimal("1") - (descuento / Decimal("100")))
        return total

    def _price_for_passenger(
        self,
        raw: dict[str, Any],
        trip_type: str,
        trip_days: int,
        age: int,
    ) -> Decimal | None:
        suffix = self._age_suffix(age)
        if trip_type == TRIP_TYPE_UNICO_VIAJE:
            return self._daily_price(raw, trip_days, suffix)
        if trip_type == TRIP_TYPE_MULTIVIAJE:
            return self._annual_price(raw, trip_days, suffix)
        if trip_type == TRIP_TYPE_LARGA_ESTADIA:
            return self._long_stay_price(raw, trip_days, suffix)
        return None

    def _age_suffix(self, age: int) -> str:
        """0-69 sin sufijo, 70-85 → _70, 86+ → _86."""
        if age >= 86:
            return "_86"
        if age >= 70:
            return "_70"
        return ""

    def _daily_brackets_for_plan(
        self, raw: dict[str, Any], age_suffix: str
    ) -> list[int]:
        """Detecta los bloques de días que trae este plan (pueden variar)."""
        if age_suffix == "_70":
            pattern = _DAILY_KEY_RE_70
        elif age_suffix == "_86":
            pattern = _DAILY_KEY_RE_86
        else:
            pattern = _DAILY_KEY_RE_BASE

        brackets: list[int] = []
        for key in raw:
            match = pattern.match(key)
            if not match:
                continue
            # Solo incluir si hay tarifa > 0 (bloques en 0 no aplican).
            if self._parse_decimal(raw.get(key)) is None:
                continue
            brackets.append(int(match.group(1)))
        brackets = sorted(set(brackets))
        return brackets or list(_DAILY_BRACKETS_DEFAULT)

    def _daily_key(self, days: int, age_suffix: str) -> str:
        return f"_{days}_dias{age_suffix}"

    def _daily_price(
        self, raw: dict[str, Any], trip_days: int, age_suffix: str
    ) -> Decimal | None:
        """
        Tarifa diaria Inter Assist:
        - Si days cae exactamente en un bloque → tarifa del bloque.
        - Si está entre dos bloques → bloque anterior + (días extra) * dia_adicional.
        - Si supera el último bloque → último + días extra * dia_adicional.
        - Si está por debajo del primer bloque → tarifa del primer bloque (mínimo).
        """
        if trip_days < 1:
            return None

        brackets = self._daily_brackets_for_plan(raw, age_suffix)
        extra_day = self._parse_decimal(raw.get(f"dia_adicional{age_suffix}"))

        if trip_days in brackets:
            return self._parse_decimal(raw.get(self._daily_key(trip_days, age_suffix)))

        if trip_days < brackets[0]:
            return self._parse_decimal(raw.get(self._daily_key(brackets[0], age_suffix)))

        if trip_days > brackets[-1]:
            base = self._parse_decimal(raw.get(self._daily_key(brackets[-1], age_suffix)))
            if base is None or extra_day is None:
                return None
            return base + (extra_day * Decimal(trip_days - brackets[-1]))

        # Entre prev y next: usar prev + días adicionales (aún no llegó al próximo bloque).
        prev = max(b for b in brackets if b < trip_days)
        base = self._parse_decimal(raw.get(self._daily_key(prev, age_suffix)))
        if base is None or extra_day is None:
            return None
        return base + (extra_day * Decimal(trip_days - prev))

    def _daily_price_formula(
        self, raw: dict[str, Any], trip_days: int, age_suffix: str
    ) -> str:
        """Texto de la fórmula aplicada (para debug)."""
        brackets = self._daily_brackets_for_plan(raw, age_suffix)
        if trip_days in brackets:
            return self._daily_key(trip_days, age_suffix)
        if trip_days < brackets[0]:
            return f"{self._daily_key(brackets[0], age_suffix)} (mínimo)"
        if trip_days > brackets[-1]:
            extra = trip_days - brackets[-1]
            return (
                f"{self._daily_key(brackets[-1], age_suffix)} + "
                f"{extra}*dia_adicional{age_suffix}"
            )
        prev = max(b for b in brackets if b < trip_days)
        extra = trip_days - prev
        return (
            f"{self._daily_key(prev, age_suffix)} + "
            f"{extra}*dia_adicional{age_suffix}"
        )

    def _annual_price(
        self, raw: dict[str, Any], trip_days: int, age_suffix: str
    ) -> Decimal | None:
        """
        Multiviaje (anual): no se cotiza por duración del viaje.
        Igual que Cardinal (maxDiasCorridos=30) / Omint (quantityOfDays):
        se usa la tarifa anual de días corridos configurada (default 30).
        """
        del trip_days  # no aplica: el producto es anual
        bracket = self._settings.interassist_annual_days
        if age_suffix:
            key = f"_{bracket}_dias{age_suffix}_anual"
        else:
            key = f"_{bracket}_dias_anual"
        return self._parse_decimal(raw.get(key))

    def _long_stay_price(
        self, raw: dict[str, Any], trip_days: int, age_suffix: str
    ) -> Decimal | None:
        months = max(3, math.ceil(trip_days / 30))
        if months <= 15:
            key = f"_{months}_meses{age_suffix}"
            return self._parse_decimal(raw.get(key))

        # Más de 15 meses: tarifa de 15 meses + días adicionales (si existen).
        base = self._parse_decimal(raw.get(f"_15_meses{age_suffix}"))
        if age_suffix == "_70":
            extra_key = "dia_adicional_70_long_stay"
        else:
            extra_key = "dia_adicional_long_stay"
        extra_day = self._parse_decimal(raw.get(extra_key))
        if base is None or extra_day is None:
            return None
        extra_days = max(0, trip_days - (15 * 30))
        return base + (extra_day * Decimal(extra_days))

    def _extract_benefits(self, raw: dict[str, Any]) -> list[Benefit]:
        items = raw.get("items") or []
        if not isinstance(items, list):
            return []
        benefits: list[Benefit] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item") or {}
            valor = entry.get("valor") or {}
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id is None:
                continue
            try:
                benefit_id = int(item_id)
            except (TypeError, ValueError):
                continue
            nombre = str(item.get("nombre") or "").strip()
            valor_txt = ""
            if isinstance(valor, dict):
                valor_txt = str(valor.get("valor") or "").strip()
            elif valor is not None:
                valor_txt = str(valor).strip()
            benefits.append(Benefit(id=benefit_id, nombre=nombre, valor=valor_txt))
        return benefits

    def _extract_coverage(
        self, raw: dict[str, Any], benefits: list[Benefit]
    ) -> Decimal:
        cobertura = self._parse_decimal(raw.get("cobertura"))
        if cobertura is not None and cobertura > 0:
            return cobertura

        for benefit in benefits:
            nombre = (benefit.nombre or "").lower()
            if "cobertura global" in nombre or "monto global" in nombre or "tope" in nombre:
                parsed = self._parse_coverage_text(benefit.valor)
                if parsed is not None:
                    return parsed
        if benefits:
            parsed = self._parse_coverage_text(benefits[0].valor)
            if parsed is not None:
                return parsed
        return Decimal("0")

    def _parse_coverage_text(self, value: str) -> Decimal | None:
        if not value:
            return None
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            return None
        try:
            return Decimal(digits)
        except InvalidOperation:
            return None

    def _parse_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float, Decimal)):
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                return None
            return amount if amount > 0 else None
        s = str(value).strip().replace(" ", "")
        if not s:
            return None
        s = s.replace(",", ".")
        try:
            amount = Decimal(s)
        except InvalidOperation:
            return None
        return amount if amount > 0 else None
