from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_admin_user, get_current_user
from app.models import User
from app.promotions.schemas import (
    PromotionCreate,
    PromotionListResponse,
    PromotionResponse,
    PromotionUpdate,
)
from app.promotions.service import (
    create_promotion,
    delete_promotion,
    get_promotion_by_id,
    list_promotions,
    promotion_to_response,
    update_promotion,
)

router = APIRouter()


@router.post("", response_model=PromotionResponse, status_code=status.HTTP_201_CREATED)
def create_promotion_route(
    data: PromotionCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> PromotionResponse:
    try:
        promotion = create_promotion(session, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return promotion_to_response(promotion)


@router.get("", response_model=PromotionListResponse)
def list_promotions_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[
        Literal["current", "upcoming", "expired", "all"],
        Query(alias="status", description="current (default), upcoming, expired o all"),
    ] = "current",
    include_inactive: Annotated[
        bool,
        Query(description="Si es true, incluye compañías y planes inactivos"),
    ] = False,
    company_id: int | None = Query(default=None),
) -> PromotionListResponse:
    items = list_promotions(
        session,
        status=status_filter,
        include_inactive=include_inactive,
        company_id=company_id,
    )
    return PromotionListResponse(
        items=[promotion_to_response(promotion, links) for promotion, links in items],
        total=len(items),
    )


@router.get("/{promotion_id}", response_model=PromotionResponse)
def get_promotion_route(
    promotion_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PromotionResponse:
    promotion = get_promotion_by_id(session, promotion_id)
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promoción no encontrada")
    return promotion_to_response(promotion)


@router.patch("/{promotion_id}", response_model=PromotionResponse)
def patch_promotion_route(
    promotion_id: int,
    data: PromotionUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> PromotionResponse:
    try:
        promotion = update_promotion(session, promotion_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promoción no encontrada")
    return promotion_to_response(promotion)


@router.delete("/{promotion_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_promotion_route(
    promotion_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
) -> None:
    if not delete_promotion(session, promotion_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promoción no encontrada")
