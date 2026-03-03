"""Adaptador mock para Pax."""
from decimal import Decimal
from uuid import uuid4

from app.quotations.schemas import Benefit, QuotePlan, QuoteRequest


class PaxQuoteProvider:
    company_name = "Pax"

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        """Mock: devuelve 2 planes de ejemplo."""
        days = (request.return_date - request.departure_date).days
        base = Decimal("50") + Decimal(str(days)) * Decimal("2.5")

        return [
            QuotePlan(
                company=self.company_name,
                id=str(uuid4()),
                plan_id="PAX-BASIC-001",
                plan_name="Pax Basic",
                coverage_amount=Decimal("50000"),
                benefits=[
                    Benefit(id=0, nombre="Asistencia médica", valor=""),
                    Benefit(id=0, nombre="Repatriación", valor=""),
                    Benefit(id=0, nombre="Pérdida de equipaje", valor=""),
                ],
                net_rate=base,
                final_rate_usd=base,
                exchange_rate=Decimal("1"),
                final_rate=base,
            ),
            QuotePlan(
                company=self.company_name,
                id=str(uuid4()),
                plan_id="PAX-PLUS-002",
                plan_name="Pax Plus",
                coverage_amount=Decimal("100000"),
                benefits=[
                    Benefit(id=0, nombre="Asistencia médica", valor=""),
                    Benefit(id=0, nombre="Repatriación", valor=""),
                    Benefit(id=0, nombre="Pérdida de equipaje", valor=""),
                    Benefit(id=0, nombre="Cobertura COVID", valor=""),
                    Benefit(id=0, nombre="Asistencia legal", valor=""),
                ],
                net_rate=base * Decimal("1.3"),
                final_rate_usd=base * Decimal("1.3"),
                exchange_rate=Decimal("1"),
                final_rate=base * Decimal("1.3"),
            ),
        ]
