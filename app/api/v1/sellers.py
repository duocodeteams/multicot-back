from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_session
from app.core.retrievable_password import decrypt_for_admin
from app.core.security import get_current_admin_or_agency_user
from app.models import User
from app.models.user import UserRole
from app.sellers.schemas import (
    SellerCreate,
    SellerListResponse,
    SellerResponse,
    SellerUpdate,
    SellerUserResponse,
)
from app.sellers.service import (
    create_seller_with_user,
    delete_seller_logical,
    get_seller_by_id,
    list_sellers,
    update_seller,
)
from sqlmodel import Session

router = APIRouter()


def _seller_to_response(seller, user, *, include_password: bool) -> SellerResponse:
    user_resp = None
    if user:
        user_resp = SellerUserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            password=decrypt_for_admin(user.password_encrypted) if include_password else None,
        )
    return SellerResponse(
        id=seller.id,
        user_id=seller.user_id,
        agency_id=seller.agency_id,
        first_name=seller.first_name,
        last_name=seller.last_name,
        address=seller.address,
        nationality=seller.nationality,
        birth_date=seller.birth_date,
        comments=seller.comments,
        commission=seller.commission,
        user=user_resp,
    )


def _get_agency_filter(current_user: User) -> int | None:
    """Si es AGENCY, retorna su agency_id. Si es ADMIN, retorna None (sin filtro)."""
    if current_user.role == UserRole.AGENCY:
        return current_user.agency_id
    return None


@router.post("", response_model=SellerResponse, status_code=status.HTTP_201_CREATED)
def create_seller(
    data: SellerCreate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_or_agency_user)],
) -> SellerResponse:
    """
    Crea vendedor con su usuario de login.
    ADMIN: puede especificar agency_id (null = independiente).
    AGENCY: el vendedor pertenece automáticamente a su agencia.
    """
    if current_user.role == UserRole.AGENCY:
        if current_user.agency_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La agencia no tiene agency_id asociado",
            )
        agency_id = current_user.agency_id
    else:
        agency_id = data.agency_id

    try:
        seller, user = create_seller_with_user(session, data, agency_id)
        return _seller_to_response(
            seller, user, include_password=(current_user.role == UserRole.ADMIN)
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("", response_model=SellerListResponse)
def list_sellers_route(
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_or_agency_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    active: bool | None = Query(
        None,
        description="Si true: solo activos. Si false: todos. Por defecto: solo activos.",
    ),
    agency_id: int | None = Query(None, description="Filtrar por agencia (solo ADMIN)"),
) -> SellerListResponse:
    """
    Lista vendedores. ADMIN: todos. AGENCY: solo los suyos.
    Filtros: active, agency_id (solo ADMIN).
    """
    agency_filter = _get_agency_filter(current_user)
    if agency_filter is not None:
        # AGENCY solo ve los suyos; ignora agency_id del query
        filter_agency_id = agency_filter
    else:
        filter_agency_id = agency_id

    active_only = active if active is not None else True

    items_tuples, total = list_sellers(
        session,
        agency_id=filter_agency_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    include_password = current_user.role == UserRole.ADMIN
    items = [
        _seller_to_response(seller, user, include_password=include_password)
        for seller, user in items_tuples
    ]
    return SellerListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{seller_id}", response_model=SellerResponse)
def get_seller(
    seller_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_or_agency_user)],
) -> SellerResponse:
    """Obtiene un vendedor por ID. ADMIN: cualquiera. AGENCY: solo los suyos."""
    agency_filter = _get_agency_filter(current_user)
    result = get_seller_by_id(session, seller_id, agency_filter)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendedor no encontrado")
    seller, user = result
    return _seller_to_response(
        seller, user, include_password=(current_user.role == UserRole.ADMIN)
    )


@router.patch("/{seller_id}", response_model=SellerResponse)
def patch_seller(
    seller_id: int,
    data: SellerUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_or_agency_user)],
) -> SellerResponse:
    """Actualiza un vendedor. agency_id no se puede modificar."""
    agency_filter = _get_agency_filter(current_user)
    seller = update_seller(session, seller_id, data, agency_filter)
    if seller is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendedor no encontrado")
    result = get_seller_by_id(session, seller_id, agency_filter)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendedor no encontrado")
    seller, user = result
    return _seller_to_response(
        seller, user, include_password=(current_user.role == UserRole.ADMIN)
    )


@router.delete("/{seller_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_seller(
    seller_id: int,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_admin_or_agency_user)],
) -> None:
    """Borrado lógico. ADMIN: cualquiera. AGENCY: solo los suyos."""
    agency_filter = _get_agency_filter(current_user)
    if not delete_seller_logical(session, seller_id, agency_filter):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendedor no encontrado")
