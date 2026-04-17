"""Orquestador: llama a todos los proveedores de cotización y unifica la respuesta."""
import logging

from app.quotations.schemas import QuotePlan, QuoteRequest, QuoteResponse
from app.services.companies.cardinal import CardinalQuoteProvider
from app.services.companies.go_assistance import GoAssistanceQuoteProvider
from app.services.companies.inter_assist import InterAssistQuoteProvider
from app.services.companies.new_travel import NewTravelQuoteProvider
from app.services.companies.pax import PaxQuoteProvider
from app.services.companies.terrawind import TerrawindQuoteProvider
from app.services.companies.universal import UniversalQuoteProvider


logger = logging.getLogger(__name__)

_PROVIDERS = [
    # PaxQuoteProvider(),
    CardinalQuoteProvider(),
    GoAssistanceQuoteProvider(),
    # TerrawindQuoteProvider(),
    NewTravelQuoteProvider(),
    InterAssistQuoteProvider(),
    UniversalQuoteProvider(),
]


def get_quotes(request: QuoteRequest) -> QuoteResponse:
    """
    Obtiene cotizaciones de todas las compañías y las devuelve en formato unificado.
    Por ahora las llamadas son secuenciales; más adelante se pueden paralelizar.
    """
    all_plans: list[QuotePlan] = []
    for provider in _PROVIDERS:
        try:
            plans = provider.get_quotes(request)
            all_plans.extend(plans)
        except Exception as exc:
            logger.warning(
                "Proveedor %s falló: %s",
                provider.company_name,
                exc,
                exc_info=True,
            )
            continue
    return QuoteResponse(plans=all_plans)
