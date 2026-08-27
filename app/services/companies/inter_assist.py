"""Adaptador para Inter Assist. Por ahora retorna lista vacía (stub)."""
from app.quotations.schemas import QuotePlan, QuoteRequest


class InterAssistQuoteProvider:
    company_name = "Inter Assist"
    company_slug = "inter_assist"

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        return []
