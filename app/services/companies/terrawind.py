"""Adaptador para Terrawind (SETW): cotización de planes en lote."""

import logging
import re
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

import httpx

from app.core.config import get_settings
from app.quotations.schemas import Benefit, QuotePlan, QuoteRequest

logger = logging.getLogger(__name__)


class TerrawindQuoteProvider:
    company_name = "Terrawind"

    def __init__(self) -> None:
        self._settings = get_settings()

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.terrawind_user or not settings.terrawind_password:
            logger.debug(
                "Terrawind: faltan TERRAWIND_USER/TERRAWIND_PASSWORD configurados"
            )
            return []

        # SETW exige el slash final en /api/v2/; sin eso responde 301.
        base_url = settings.terrawind_base_url.strip()
        plans: list[QuotePlan] = []

        with httpx.Client(
            timeout=30.0,
            auth=(settings.terrawind_user, settings.terrawind_password),
            follow_redirects=True,
        ) as client:
            products = self._get_products(client, base_url)
            if not products:
                return []

            # TODO: filtrar manualmente por IDs/nombres de planes habilitados.
            for product in products:
                try:
                    plan = self._quote_product(
                        client=client,
                        base_url=base_url,
                        product=product,
                        request=request,
                    )
                except Exception:
                    logger.warning(
                        "Terrawind: error inesperado al cotizar product_id=%s",
                        product.get("product_id"),
                        exc_info=True,
                    )
                    continue

                if plan is not None:
                    plans.append(plan)

        return plans

    def _quote_product(
        self,
        client: httpx.Client,
        base_url: str,
        product: dict[str, str],
        request: QuoteRequest,
    ) -> QuotePlan | None:
        product_id = product.get("product_id", "").strip()
        if not product_id:
            return None

        product_name = product.get("product_name", "").strip() or product_id
        benefits = self._get_coverages(client, base_url, product_id)
        price_data = self._get_voucher_price(client, base_url, product_id, request)
        if not price_data:
            return None

        final_rate_usd = self._parse_decimal(price_data.get("price"))
        if final_rate_usd is None:
            logger.debug("Terrawind: plan %s sin price parseable", product_id)
            return None

        commission = self._parse_decimal(price_data.get("comission_price")) or Decimal("0")
        net_rate = (final_rate_usd - commission).quantize(Decimal("0.01"))

        coverage_amount = self._extract_coverage_from_benefits(benefits)

        return QuotePlan(
            company=self.company_name,
            id=product_id,
            plan_id=product_id,
            plan_name=product_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=net_rate,
            final_rate_usd=final_rate_usd,
            exchange_rate=Decimal("1"),
            final_rate=final_rate_usd,
        )

    def _get_products(self, client: httpx.Client, base_url: str) -> list[dict[str, str]]:
        root = self._call_action(client, base_url, "get_products", {})
        if root is None:
            return []
        return [self._xml_child_map(node) for node in root.findall("product")]

    def _get_coverages(
        self,
        client: httpx.Client,
        base_url: str,
        product_id: str,
    ) -> list[Benefit]:
        root = self._call_action(
            client,
            base_url,
            "get_coverages",
            {"product_id": product_id},
        )
        if root is None:
            return []

        benefits: list[Benefit] = []
        for node in root.findall("coverage"):
            fields = self._xml_child_map(node)
            coverage_id = fields.get("coverage_id")
            if not coverage_id:
                continue
            try:
                parsed_id = int(coverage_id)
            except ValueError:
                continue

            benefits.append(
                Benefit(
                    id=parsed_id,
                    nombre=fields.get("coverage_name", "").strip(),
                    valor=fields.get("coverage_val", "").strip(),
                )
            )
        return benefits

    def _get_voucher_price(
        self,
        client: httpx.Client,
        base_url: str,
        product_id: str,
        request: QuoteRequest,
    ) -> dict[str, str] | None:
        params: dict[str, str] = {
            "product_id": product_id,
            "passengers": str(len(request.ages)),
            "date_from": request.departure_date.strftime("%d/%m/%Y"),
            "date_to": request.return_date.strftime("%d/%m/%Y"),
        }
        for idx, age in enumerate(request.ages):
            params[f"passengers_ages[{idx}]age"] = str(age)

        root = self._call_action(client, base_url, "get_voucher_price", params)
        if root is None:
            return None
        return self._xml_child_map(root)

    def _call_action(
        self,
        client: httpx.Client,
        base_url: str,
        action: str,
        params: dict[str, str],
    ) -> ET.Element | None:
        request_params = {"action": action, **params}
        response = client.get(base_url, params=request_params)
        if response.status_code >= 400:
            logger.error(
                "Terrawind: action=%s HTTP %s - body=%s",
                action,
                response.status_code,
                response.text,
            )
        response.raise_for_status()

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            logger.error(
                "Terrawind: XML inválido en action=%s. body=%s",
                action,
                response.text,
            )
            return None

        error = root.find("error")
        if error is not None:
            error_number = (error.findtext("error_number") or "").strip()
            error_description = (error.findtext("error_description") or "").strip()
            logger.warning(
                "Terrawind: action=%s error=%s desc=%s params=%s",
                action,
                error_number,
                error_description,
                params,
            )
            return None
        return root

    def _xml_child_map(self, node: ET.Element) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in list(node):
            text = child.text or ""
            result[child.tag] = text.strip()
        return result

    def _parse_decimal(self, raw_value: str | None) -> Decimal | None:
        if raw_value is None:
            return None
        value = raw_value.strip().replace(" ", "")
        if not value:
            return None

        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")

        try:
            return Decimal(value).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None

    def _extract_coverage_from_benefits(self, benefits: list[Benefit]) -> Decimal:
        # Regla acordada para Terrawind: priorizar el primer benefit.
        if benefits:
            first_amount = self._parse_usd_coverage(benefits[0].valor)
            if first_amount is not None:
                return first_amount

        primary_keywords = ("monto global", "tope", "cobertura maxima")
        medical_keywords = (
            "asistencia medica",
            "asistencia médica",
            "gastos medicos",
            "gastos médicos",
        )

        for benefit in benefits:
            name = benefit.nombre.strip().lower()
            if any(keyword in name for keyword in primary_keywords):
                parsed = self._parse_usd_coverage(benefit.valor)
                if parsed is not None:
                    return parsed

        for benefit in benefits:
            name = benefit.nombre.strip().lower()
            if any(keyword in name for keyword in medical_keywords):
                parsed = self._parse_usd_coverage(benefit.valor)
                if parsed is not None:
                    return parsed

        for benefit in benefits:
            parsed = self._parse_usd_coverage(benefit.valor)
            if parsed is not None:
                return parsed

        return Decimal("0")

    def _parse_usd_coverage(self, raw_value: str) -> Decimal | None:
        if not raw_value:
            return None
        upper_value = raw_value.upper()
        if "USD" not in upper_value and "U$S" not in upper_value:
            return None
        match = re.search(r"(\d[\d\.,]*)", raw_value)
        if not match:
            return None
        digits_only = re.sub(r"\D", "", match.group(1))
        if not digits_only:
            return None
        try:
            return Decimal(digits_only)
        except InvalidOperation:
            return None
