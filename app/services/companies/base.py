from typing import Protocol

from app.quotations.schemas import QuotePlan, QuoteRequest


class QuoteProvider(Protocol):
    """Interfaz que debe implementar cada adaptador de compañía."""

    @property
    def company_name(self) -> str:
        ...

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        """Obtiene cotizaciones para el request. Puede retornar lista vacía si no hay ofertas."""
        ...
