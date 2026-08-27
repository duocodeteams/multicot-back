"""Aplica whitelist y markup del catálogo local sobre planes cotizados."""

from collections import defaultdict
from decimal import Decimal

from sqlmodel import Session, select

from app.models.company import Company
from app.models.plan import Plan, PlanDestination
from app.quotations.schemas import QuotePlan


class CompanyCatalog:
    def __init__(
        self,
        company: Company,
        plans_by_external_id: dict[str, Plan],
        enabled_destinations: dict[int, set[int]],
        has_any_plan: bool,
    ) -> None:
        self.company = company
        self.plans_by_external_id = plans_by_external_id
        self.enabled_destinations = enabled_destinations
        self.has_any_plan = has_any_plan


def load_catalogs(session: Session) -> dict[str, CompanyCatalog]:
    companies = session.exec(select(Company)).all()
    plans = session.exec(select(Plan)).all()
    destinations = session.exec(select(PlanDestination)).all()

    plans_by_company: dict[int, list[Plan]] = defaultdict(list)
    for plan in plans:
        plans_by_company[plan.company_id].append(plan)

    enabled_by_plan: dict[int, set[int]] = defaultdict(set)
    for row in destinations:
        if row.enabled:
            enabled_by_plan[row.plan_id].add(row.destination_id)

    catalogs: dict[str, CompanyCatalog] = {}
    for company in companies:
        company_plans = plans_by_company.get(company.id or 0, [])
        catalogs[company.slug] = CompanyCatalog(
            company=company,
            plans_by_external_id={
                plan.external_plan_id: plan
                for plan in company_plans
                if plan.active
            },
            enabled_destinations={
                plan.id: enabled_by_plan.get(plan.id or 0, set())
                for plan in company_plans
                if plan.id is not None
            },
            has_any_plan=bool(company_plans),
        )
    return catalogs


def apply_markup(plan: QuotePlan, markup: Decimal) -> QuotePlan:
    """Incrementa el PVP que trajo el adapter (`final_rate`). No toca `net_rate`.

    Si el adapter mapeó neto y PVP al mismo valor (Terrawind), el markup
    queda aplicado sobre el neto. En el resto, sobre el PVP de la API.
    Markup 0 deja el PVP igual. `markup` en la respuesta es el total aplicado.
    """
    factor = Decimal("1") + (markup / Decimal("100"))
    updates: dict[str, Decimal] = {
        "markup": markup,
        "final_rate": (plan.final_rate * factor).quantize(Decimal("0.01")),
    }
    if plan.final_rate_usd is not None:
        updates["final_rate_usd"] = (plan.final_rate_usd * factor).quantize(
            Decimal("0.01")
        )
    return plan.model_copy(update=updates)


def filter_and_markup_plans(
    catalog: CompanyCatalog | None,
    destination_id: int,
    quoted_plans: list[QuotePlan],
) -> list[QuotePlan]:
    if catalog is None or not catalog.has_any_plan:
        return quoted_plans

    result: list[QuotePlan] = []
    for quoted in quoted_plans:
        local_plan = catalog.plans_by_external_id.get(quoted.plan_id)
        if local_plan is None or local_plan.id is None:
            continue
        if destination_id not in catalog.enabled_destinations.get(local_plan.id, set()):
            continue
        result.append(apply_markup(quoted, local_plan.total_markup))
    return result
