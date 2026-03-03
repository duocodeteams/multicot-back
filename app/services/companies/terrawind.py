"""Adaptador para Terrawind. Por ahora retorna lista vacía (stub)."""
from app.quotations.schemas import QuotePlan, QuoteRequest


class TerrawindQuoteProvider:
    company_name = "Terrawind"

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        return []
