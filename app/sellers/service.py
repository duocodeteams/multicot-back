from decimal import Decimal

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.retrievable_password import encrypt_for_storage
from app.core.security import hash_password
from app.models import Seller, User
from app.models.user import UserRole

from .schemas import SellerCreate, SellerUpdate


def create_seller_with_user(
    session: Session,
    data: SellerCreate,
    agency_id: int | None,
) -> tuple[Seller, User]:
    """
    Crea vendedor + usuario en una transacción.
    agency_id: obligatorio si lo crea una agencia; opcional (None = independiente) si lo crea ADMIN.
    """
    existing = session.exec(select(User).where(User.email == data.user.email)).first()
    if existing:
        raise ValueError(f"El email {data.user.email} ya está registrado")

    user = User(
        email=data.user.email,
        password_hash=hash_password(data.user.password),
        password_encrypted=encrypt_for_storage(data.user.password),
        role=UserRole.SELLER,
    )
    session.add(user)
    session.flush()

    seller = Seller(
        user_id=user.id,
        agency_id=agency_id,
        first_name=data.first_name,
        last_name=data.last_name,
        address=data.address,
        nationality=data.nationality,
        birth_date=data.birth_date,
        comments=data.comments or "",
        commission=data.commission or Decimal("0"),
    )
    session.add(seller)
    session.commit()
    session.refresh(seller)
    session.refresh(user)

    return seller, user


def list_sellers(
    session: Session,
    agency_id: int | None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[Seller, User]], int]:
    """
    Lista vendedores. Si agency_id no es None, filtra por esa agencia.
    active_only: si True, solo activos; si False, todos.
    Retorna (lista de (seller, user), total).
    """
    stmt = select(Seller).order_by(Seller.id)
    if active_only:
        stmt = stmt.where(Seller.active == True)
    if agency_id is not None:
        stmt = stmt.where(Seller.agency_id == agency_id)

    count_stmt = select(func.count()).select_from(Seller)
    if active_only:
        count_stmt = count_stmt.where(Seller.active == True)
    if agency_id is not None:
        count_stmt = count_stmt.where(Seller.agency_id == agency_id)

    count_result = session.exec(count_stmt).one()
    total = count_result[0] if isinstance(count_result, tuple) else count_result

    sellers = session.exec(stmt.offset(offset).limit(limit)).all()
    result: list[tuple[Seller, User]] = []
    for seller in sellers:
        user = session.get(User, seller.user_id)
        if user:
            result.append((seller, user))

    return result, total


def get_seller_by_id(
    session: Session,
    seller_id: int,
    agency_id: int | None,
) -> tuple[Seller, User] | None:
    """
    Obtiene un vendedor por ID. Si agency_id no es None, verifica que pertenezca a esa agencia.
    Retorna None si no existe, está inactivo, o no pertenece a la agencia.
    """
    seller = session.get(Seller, seller_id)
    if seller is None or not seller.active:
        return None
    if agency_id is not None and seller.agency_id != agency_id:
        return None

    user = session.get(User, seller.user_id)
    if user is None:
        return None
    return seller, user


def update_seller(
    session: Session,
    seller_id: int,
    data: SellerUpdate,
    agency_id: int | None,
) -> Seller | None:
    """
    Actualiza un vendedor. Si agency_id no es None, verifica que pertenezca a esa agencia.
    agency_id del seller NO se modifica (inmutable).
    """
    seller = session.get(Seller, seller_id)
    if seller is None or not seller.active:
        return None
    if agency_id is not None and seller.agency_id != agency_id:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(seller, key, value)

    session.add(seller)
    session.commit()
    session.refresh(seller)
    return seller


def delete_seller_logical(
    session: Session,
    seller_id: int,
    agency_id: int | None,
) -> bool:
    """
    Borrado lógico: desactiva seller y su user.
    Si agency_id no es None, verifica que pertenezca a esa agencia.
    """
    seller = session.get(Seller, seller_id)
    if seller is None or not seller.active:
        return False
    if agency_id is not None and seller.agency_id != agency_id:
        return False

    seller.active = False
    user = session.get(User, seller.user_id)
    if user:
        user.active = False
        session.add(user)
    session.add(seller)
    session.commit()
    return True
