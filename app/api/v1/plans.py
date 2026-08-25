from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_admin_user
from app.models import User
from app.plans.schemas import PlanCreate, PlanListResponse, PlanResponse, PlanUpdate
from app.plans.service import (
    create_plan,
    delete_plan_logical,
    get_plan_by_id,
    list_plans,
    plan_to_response,
    update_plan,
)
from app.quotations.schemas import DESTINO_IDS_VALIDOS

router = APIRouter()


def _parse_destination_id(destination_id: int | None) -> int | None:
    if destination_id is None:
        return None
    if destination_id not in DESTINO_IDS_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="destination_id debe ser 1-5",
        )
    return destination_id


@router.post("", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
def create_plan_route(
    data: PlanCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> PlanResponse:
    try:
        plan = create_plan(session, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return plan_to_response(plan)


@router.get("", response_model=PlanListResponse)
def list_plans_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    company_id: int | None = Query(None),
    destination_id: int | None = Query(None, description="Solo planes habilitados en ese destino (1-5)"),
    active: bool | None = Query(
        None,
        description="Si true o se omite: solo activos. Si false: todos.",
    ),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlanListResponse:
    destination_id = _parse_destination_id(destination_id)
    active_only = True if active is None else active
    items, total = list_plans(
        session,
        company_id=company_id,
        destination_id=destination_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return PlanListResponse(
        items=[plan_to_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{plan_id}", response_model=PlanResponse)
def get_plan_route(
    plan_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> PlanResponse:
    plan = get_plan_by_id(session, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan no encontrado")
    return plan_to_response(plan)


@router.patch("/{plan_id}", response_model=PlanResponse)
def patch_plan_route(
    plan_id: int,
    data: PlanUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> PlanResponse:
    plan = update_plan(session, plan_id, data)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan no encontrado")
    return plan_to_response(plan)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan_route(
    plan_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> None:
    if not delete_plan_logical(session, plan_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan no encontrado")
