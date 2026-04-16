"""Adaptador para Inter Assist. Por ahora retorna lista vacía (stub)."""
from app.quotations.schemas import QuotePlan, QuoteRequest


class InterAssistQuoteProvider:
    company_name = "Inter Assist"

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        return []
