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

Multiviaje: no usa las fechas del viaje. Usa request.days_range (30 / 60 / 90)
como días corridos por viaje, según el schema:
  30 → _30_dias_anual, 60 → _60_dias_anual, 90 → _90_dias_anual.
El plan es el mismo; solo cambia la key. Si esa key está en 0, no se cotiza.

Promociones (promociones_object):
- Precio lista → base_rate / base_rate_usd
- Precio con promo → final_rate / final_rate_usd
- % y nombre → discount_pct / promotion_name
Si no hay promo activa, base_rate queda null (solo final_rate).

IMPORTANTE: no usar POST /api/ventas para cotizar (emite vouchers).
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import get_settings
from app.quotations.schemas import (
    DAYS_RANGE_VALIDOS,
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
    company_slug = "inter_assist"

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
            except Exception:
                logger.exception(
                    "InterAssist: error mapeando plan id=%s", raw.get("id")
                )
                continue
            if plan:
                plans.append(plan)

        return plans

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

    def _annual_price_keys(self, bracket: int, age_suffix: str) -> tuple[str, ...]:
        if age_suffix:
            return (
                f"_{bracket}_dias{age_suffix}_anual",
                f"_{bracket}_dias_anual{age_suffix}",
            )
        return (f"_{bracket}_dias_anual",)

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

        # Precio de lista (sin promociones_object). El descuento legacy del plan
        # solo aplica si no hay promo activa.
        list_total = self._calculate_list_price(raw, request, trip_days)
        if list_total is None or list_total <= 0:
            return None

        promo = self._select_active_promotion(raw, reference_date=date.today())
        discount_pct: Decimal | None = None
        promotion_name: str | None = None
        if promo is not None:
            discount_pct, promotion_name = promo
            final_total = (
                list_total * (Decimal("1") - (discount_pct / Decimal("100")))
            ).quantize(Decimal("0.01"))
        else:
            plan_descuento = self._parse_decimal(raw.get("descuento")) or Decimal("0")
            if plan_descuento > 0:
                discount_pct = plan_descuento.quantize(Decimal("0.01"))
                final_total = (
                    list_total * (Decimal("1") - (plan_descuento / Decimal("100")))
                ).quantize(Decimal("0.01"))
            else:
                final_total = list_total

        plan_name = str(raw.get("nombre") or "").strip() or str(plan_id)
        benefits = self._extract_benefits(raw)
        coverage_amount = self._extract_coverage(raw, benefits)
        list_rate_usd = list_total.quantize(Decimal("0.01"))
        final_rate_usd = final_total.quantize(Decimal("0.01"))

        # La tarifa del plan está en la moneda indicada por "moneda" (ej. Dólar).
        # local_currency_conversion funciona como TC a moneda local cuando > 1.
        #   - Si hay TC válido → final_rate = USD * TC (moneda local)
        #   - Si no hay TC     → final_rate = USD
        # Temporal: exchange_rate fijo en 2 (independiente del cálculo de final_rate).
        lcc = self._parse_decimal(raw.get("local_currency_conversion"))
        if lcc is not None and lcc > 1:
            fx = lcc.quantize(Decimal("0.0001"))
            list_rate = (list_rate_usd * fx).quantize(Decimal("0.01"))
            final_rate = (final_rate_usd * fx).quantize(Decimal("0.01"))
        else:
            list_rate = list_rate_usd
            final_rate = final_rate_usd
        exchange_rate = Decimal("2")
        net_rate = final_rate

        has_promo_price = discount_pct is not None and final_rate < list_rate
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
            base_rate_usd=list_rate_usd if has_promo_price else None,
            base_rate=list_rate if has_promo_price else None,
            discount_pct=discount_pct if has_promo_price else None,
            promotion_name=promotion_name if has_promo_price else None,
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

    def _calculate_list_price(
        self,
        raw: dict[str, Any],
        request: QuoteRequest,
        trip_days: int,
    ) -> Decimal | None:
        """Suma tarifas de tabla (sin descuento del plan ni promociones_object)."""
        total = Decimal("0")
        for age in request.ages:
            price = self._price_for_passenger(raw, request, trip_days, age)
            if price is None:
                return None
            total += price
        return total

    def _select_active_promotion(
        self, raw: dict[str, Any], reference_date: date
    ) -> tuple[Decimal, str] | None:
        """
        Elige la mejor promo activa de promociones_object (mayor %).
        Solo tipo porcentaje. Valida activo + ventana programada.
        """
        items = raw.get("promociones_object") or []
        if not isinstance(items, list):
            return None

        best_pct: Decimal | None = None
        best_name: str | None = None
        for entry in items:
            if not isinstance(entry, dict):
                continue
            if entry.get("activo") is False:
                continue

            tipo = entry.get("tipo_promocion") or {}
            tipo_nombre = ""
            if isinstance(tipo, dict):
                tipo_nombre = str(tipo.get("nombre") or "").strip().lower()
            if tipo_nombre and "porcentaje" not in tipo_nombre:
                continue

            pct = self._parse_decimal(entry.get("porcentaje"))
            if pct is None or pct <= 0 or pct >= 100:
                continue

            if entry.get("programado"):
                start = self._parse_api_date(entry.get("fecha_inicial"))
                end = self._parse_api_date(entry.get("fecha_final"))
                if start is not None and reference_date < start:
                    continue
                if end is not None and reference_date > end:
                    continue

            nombre = str(entry.get("nombre") or "").strip() or f"{pct}% OFF"
            if best_pct is None or pct > best_pct:
                best_pct = pct
                best_name = nombre

        if best_pct is None or best_name is None:
            return None
        return best_pct.quantize(Decimal("0.01")), best_name

    def _parse_api_date(self, value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        s = str(value).strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).date()
        except ValueError:
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                return None

    def _price_for_passenger(
        self,
        raw: dict[str, Any],
        request: QuoteRequest,
        trip_days: int,
        age: int,
    ) -> Decimal | None:
        suffix = self._age_suffix(age)
        if request.trip_type == TRIP_TYPE_UNICO_VIAJE:
            return self._daily_price(raw, trip_days, suffix)
        if request.trip_type == TRIP_TYPE_MULTIVIAJE:
            return self._annual_price(raw, request.days_range, suffix)
        if request.trip_type == TRIP_TYPE_LARGA_ESTADIA:
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

    def _annual_price(
        self, raw: dict[str, Any], days_range: int | None, age_suffix: str
    ) -> Decimal | None:
        """Tarifa anual exacta: 30 → _30_dias_anual, 60 → _60_dias_anual, 90 → _90_dias_anual."""
        if days_range not in DAYS_RANGE_VALIDOS:
            return None
        for key in self._annual_price_keys(days_range, age_suffix):
            price = self._parse_decimal(raw.get(key))
            if price is not None:
                return price
        return None

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
