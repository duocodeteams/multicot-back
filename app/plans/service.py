from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.models.company import Company
from app.models.plan import Plan, PlanDestination
from app.quotations.schemas import DESTINO_IDS_VALIDOS

from .schemas import PlanCreate, PlanResponse, PlanUpdate, PlanDestinationResponse


def _plan_options():
    return (
        selectinload(Plan.company),
        selectinload(Plan.destinations),
    )


def _to_response(plan: Plan) -> PlanResponse:
    destinations = sorted(plan.destinations, key=lambda item: item.destination_id)
    return PlanResponse(
        id=plan.id,
        company_id=plan.company_id,
        company_slug=plan.company.slug,
        company_name=plan.company.name,
        external_plan_id=plan.external_plan_id,
        name=plan.name,
        markup=plan.markup,
        active=plan.active,
        destinations=[
            PlanDestinationResponse(
                destination_id=item.destination_id,
                enabled=item.enabled,
            )
            for item in destinations
        ],
    )


def _get_plan(session: Session, plan_id: int) -> Plan | None:
    return session.exec(
        select(Plan).where(Plan.id == plan_id).options(*_plan_options())
    ).first()


def create_plan(session: Session, data: PlanCreate) -> Plan:
    company = session.get(Company, data.company_id)
    if company is None:
        raise ValueError("Compañía no encontrada")

    existing = session.exec(
        select(Plan).where(
            Plan.company_id == data.company_id,
            Plan.external_plan_id == data.external_plan_id,
        )
    ).first()
    if existing is not None:
        if not existing.active:
            raise ValueError(
                "Ya existe un plan inactivo con ese ID externo. Reactivalo en lugar de crear otro."
            )
        raise ValueError("Ya existe un plan con ese ID externo para la compañía")

    plan = Plan(
        company_id=data.company_id,
        external_plan_id=data.external_plan_id,
        name=data.name,
        markup=data.markup if data.markup is not None else Decimal("0"),
        active=True,
    )
    session.add(plan)
    session.flush()

    for destination_id in DESTINO_IDS_VALIDOS:
        session.add(
            PlanDestination(
                plan_id=plan.id,
                destination_id=destination_id,
                enabled=False,
            )
        )
    session.commit()

    created = _get_plan(session, plan.id)
    if created is None:
        raise RuntimeError("No se pudo recargar el plan creado")
    return created


def list_plans(
    session: Session,
    company_id: int | None = None,
    destination_id: int | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Plan], int]:
    stmt = select(Plan).options(*_plan_options()).order_by(Plan.id)
    count_stmt = select(func.count()).select_from(Plan)

    if company_id is not None:
        stmt = stmt.where(Plan.company_id == company_id)
        count_stmt = count_stmt.where(Plan.company_id == company_id)
    if active_only:
        stmt = stmt.where(Plan.active == True)  # noqa: E712
        count_stmt = count_stmt.where(Plan.active == True)  # noqa: E712
    if destination_id is not None:
        dest_filter = (
            PlanDestination.destination_id == destination_id,
            PlanDestination.enabled == True,  # noqa: E712
        )
        stmt = stmt.join(PlanDestination).where(*dest_filter)
        count_stmt = count_stmt.join(PlanDestination).where(*dest_filter)

    count_result = session.exec(count_stmt).one()
    total = count_result[0] if isinstance(count_result, tuple) else count_result
    items = list(session.exec(stmt.offset(offset).limit(limit)).all())
    return items, total


def get_plan_by_id(session: Session, plan_id: int) -> Plan | None:
    return _get_plan(session, plan_id)


def update_plan(session: Session, plan_id: int, data: PlanUpdate) -> Plan | None:
    plan = session.get(Plan, plan_id)
    if plan is None:
        return None

    update_data = data.model_dump(exclude_unset=True, exclude={"destinations"})
    for key, value in update_data.items():
        setattr(plan, key, value)

    if data.destinations is not None:
        current = {
            item.destination_id: item
            for item in session.exec(
                select(PlanDestination).where(PlanDestination.plan_id == plan_id)
            ).all()
        }
        for item in data.destinations:
            row = current.get(item.destination_id)
            if row is None:
                session.add(
                    PlanDestination(
                        plan_id=plan_id,
                        destination_id=item.destination_id,
                        enabled=item.enabled,
                    )
                )
            else:
                row.enabled = item.enabled
                session.add(row)

    session.add(plan)
    session.commit()
    return _get_plan(session, plan_id)


def delete_plan_logical(session: Session, plan_id: int) -> bool:
    plan = session.get(Plan, plan_id)
    if plan is None or not plan.active:
        return False
    plan.active = False
    session.add(plan)
    session.commit()
    return True


def plan_to_response(plan: Plan) -> PlanResponse:
    return _to_response(plan)
