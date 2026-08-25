"""Orquestador: llama a los proveedores activos y aplica catálogo (filtro + markup)."""
import logging

from sqlmodel import Session

from app.plans.catalog import filter_and_markup_plans, load_catalogs
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
    PaxQuoteProvider(),
    CardinalQuoteProvider(),
    GoAssistanceQuoteProvider(),
    TerrawindQuoteProvider(),
    NewTravelQuoteProvider(),
    InterAssistQuoteProvider(),
    UniversalQuoteProvider(),
]


def get_quotes(session: Session, request: QuoteRequest) -> QuoteResponse:
    """
    Obtiene cotizaciones de las compañías activas y las devuelve unificadas.
    El catálogo local filtra por destino y aplica markup cuando hay planes cargados.
    """
    catalogs = load_catalogs(session)
    all_plans: list[QuotePlan] = []
    for provider in _PROVIDERS:
        catalog = catalogs.get(provider.company_slug)
        if catalog is None or not catalog.company.active:
            logger.debug(
                "Proveedor %s omitido: compañía inactiva o sin seed",
                provider.company_slug,
            )
            continue
        try:
            plans = provider.get_quotes(request)
        except Exception as exc:
            logger.warning(
                "Proveedor %s falló: %s",
                provider.company_name,
                exc,
                exc_info=True,
            )
            continue
        plans = filter_and_markup_plans(catalog, request.destination_id, plans)
        all_plans.extend(plans)
    return QuoteResponse(plans=all_plans)
