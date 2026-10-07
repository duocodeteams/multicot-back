from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.models.company import Company
from app.models.plan import Plan
from app.models.promotion import Promotion, PromotionPlan

from .schemas import (
    PromotionCreate,
    PromotionPlanResponse,
    PromotionResponse,
    PromotionStatus,
    PromotionUpdate,
)

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def argentina_today() -> date:
    return datetime.now(ARGENTINA_TZ).date()


def _promotion_options():
    return (
        selectinload(Promotion.company),
        selectinload(Promotion.plan_links).selectinload(PromotionPlan.plan),
    )


def _sorted_links(links: list[PromotionPlan]) -> list[PromotionPlan]:
    return sorted(links, key=lambda link: (link.plan.name.casefold(), link.plan.id or 0))


def _links_for_response(promotion: Promotion, include_inactive: bool) -> list[PromotionPlan]:
    links = list(promotion.plan_links)
    if not include_inactive:
        links = [link for link in links if link.plan.active]
    return _sorted_links(links)


def _to_response(promotion: Promotion, links: list[PromotionPlan]) -> PromotionResponse:
    return PromotionResponse(
        id=promotion.id,
        company_id=promotion.company_id,
        company_slug=promotion.company.slug,
        company_name=promotion.company.name,
        company_active=promotion.company.active,
        description=promotion.description,
        starts_on=promotion.starts_on,
        ends_on=promotion.ends_on,
        plans=[
            PromotionPlanResponse(
                id=link.plan.id,
                name=link.plan.name,
                external_plan_id=link.plan.external_plan_id,
                active=link.plan.active,
            )
            for link in links
        ],
    )


def _get_promotion(session: Session, promotion_id: int) -> Promotion | None:
    return session.exec(
        select(Promotion).where(Promotion.id == promotion_id).options(*_promotion_options())
    ).first()


def _require_plans(session: Session, company_id: int, plan_ids: list[int]) -> None:
    plans = list(session.exec(select(Plan).where(Plan.id.in_(plan_ids))).all())
    found = {plan.id: plan for plan in plans}
    if any(plan_id not in found for plan_id in plan_ids):
        raise ValueError("Uno o más planes no existen")
    if any(found[plan_id].company_id != company_id for plan_id in plan_ids):
        raise ValueError("Los planes deben pertenecer a la compañía de la promo")
    if any(not found[plan_id].active for plan_id in plan_ids):
        raise ValueError("Los planes deben estar activos")


def _replace_plans(promotion: Promotion, plan_ids: list[int]) -> None:
    desired = set(plan_ids)
    for row in list(promotion.plan_links):
        if row.plan_id not in desired:
            promotion.plan_links.remove(row)
    existing = {row.plan_id for row in promotion.plan_links}
    for plan_id in plan_ids:
        if plan_id not in existing:
            promotion.plan_links.append(PromotionPlan(promotion_id=promotion.id, plan_id=plan_id))


def create_promotion(session: Session, data: PromotionCreate) -> Promotion:
    company = session.get(Company, data.company_id)
    if company is None:
        raise ValueError("Compañía no encontrada")
    _require_plans(session, data.company_id, data.plan_ids)

    promotion = Promotion(
        company_id=data.company_id,
        description=data.description,
        starts_on=data.starts_on,
        ends_on=data.ends_on,
    )
    session.add(promotion)
    session.flush()
    for plan_id in data.plan_ids:
        session.add(PromotionPlan(promotion_id=promotion.id, plan_id=plan_id))
    session.commit()

    created = _get_promotion(session, promotion.id)
    if created is None:
        raise RuntimeError("No se pudo recargar la promo creada")
    return created


def list_promotions(
    session: Session,
    status: PromotionStatus = "current",
    include_inactive: bool = False,
    company_id: int | None = None,
) -> list[tuple[Promotion, list[PromotionPlan]]]:
    today = argentina_today()
    stmt = (
        select(Promotion)
        .join(Company)
        .options(*_promotion_options())
        .order_by(Company.name, Promotion.ends_on, Promotion.id)
    )
    if company_id is not None:
        stmt = stmt.where(Promotion.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(Company.active == True)  # noqa: E712
    if status == "current":
        stmt = stmt.where(Promotion.starts_on <= today, Promotion.ends_on >= today)
    elif status == "upcoming":
        stmt = stmt.where(Promotion.starts_on > today)
    elif status == "expired":
        stmt = stmt.where(Promotion.ends_on < today)

    items = list(session.exec(stmt).unique().all())
    visible: list[tuple[Promotion, list[PromotionPlan]]] = []
    for promotion in items:
        links = _links_for_response(promotion, include_inactive)
        if not links:
            continue
        visible.append((promotion, links))
    return visible


def get_promotion_by_id(session: Session, promotion_id: int) -> Promotion | None:
    return _get_promotion(session, promotion_id)


def update_promotion(session: Session, promotion_id: int, data: PromotionUpdate) -> Promotion | None:
    promotion = session.get(Promotion, promotion_id)
    if promotion is None:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for field in ("description", "starts_on", "ends_on", "plan_ids"):
        if field in update_data and update_data[field] is None:
            raise ValueError(f"{field} no puede ser nulo")

    starts_on = update_data.get("starts_on", promotion.starts_on)
    ends_on = update_data.get("ends_on", promotion.ends_on)
    if ends_on < starts_on:
        raise ValueError("ends_on debe ser posterior o igual a starts_on")

    if "description" in update_data:
        promotion.description = update_data["description"]
    if "starts_on" in update_data:
        promotion.starts_on = update_data["starts_on"]
    if "ends_on" in update_data:
        promotion.ends_on = update_data["ends_on"]
    if "plan_ids" in update_data:
        _require_plans(session, promotion.company_id, update_data["plan_ids"])
        _replace_plans(promotion, update_data["plan_ids"])

    session.add(promotion)
    session.commit()
    return _get_promotion(session, promotion_id)


def delete_promotion(session: Session, promotion_id: int) -> bool:
    promotion = session.get(Promotion, promotion_id)
    if promotion is None:
        return False
    session.delete(promotion)
    session.commit()
    return True


def promotion_to_response(
    promotion: Promotion,
    links: list[PromotionPlan] | None = None,
) -> PromotionResponse:
    if links is None:
        links = _sorted_links(list(promotion.plan_links))
    return _to_response(promotion, links)
