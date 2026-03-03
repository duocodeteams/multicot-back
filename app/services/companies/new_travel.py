"""Adaptador para New Travel. Por ahora retorna lista vacía (stub)."""
from app.quotations.schemas import QuotePlan, QuoteRequest


class NewTravelQuoteProvider:
    company_name = "New Travel"

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        return []
