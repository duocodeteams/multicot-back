from sqlalchemy import func
from sqlmodel import Session, select

from app.core.security import hash_password
from app.models import Agency, Seller, User
from app.models.user import UserRole

from .schemas import AgencyCreate, AgencyUpdate


def create_agency_with_user(session: Session, data: AgencyCreate) -> tuple[Agency, User]:
    """
    Crea agencia + usuario principal en una transacción.
    Nunca deja estados intermedios rotos.
    """
    existing = session.exec(select(User).where(User.email == data.user.email)).first()
    if existing:
        raise ValueError(f"El email {data.user.email} ya está registrado")

    agency = Agency(
        name=data.name,
        legal_name=data.legal_name,
        tax_id=data.tax_id,
        address=data.address,
        country=data.country,
        legal_representative_name=data.legal_representative_name,
        agency_email=data.agency_email,
        administration_email=data.administration_email,
        office_phone=data.office_phone,
        contact_name=data.contact_name,
        contact_email=data.contact_email,
        contact_phone=data.contact_phone,
        activation_date=data.activation_date,
        commission=data.commission,
        billing_frequency=data.billing_frequency,
        payment_method=data.payment_method,
        tax_condition=data.tax_condition,
        bank_account=data.bank_account,
        ssn_register=data.ssn_register,
    )
    session.add(agency)
    session.flush()

    user = User(
        email=data.user.email,
        password_hash=hash_password(data.user.password),
        role=UserRole.AGENCY,
        agency_id=agency.id,
    )
    session.add(user)
    session.commit()
    session.refresh(agency)
    session.refresh(user)

    return agency, user


def list_agencies(
    session: Session,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[Agency, User | None]], int]:
    """
    Lista agencias activas con paginación limit/offset.
    Retorna (lista de (agency, user_principal), total).
    """
    stmt = select(Agency).where(Agency.active == True).order_by(Agency.id)
    count_result = session.exec(
        select(func.count()).select_from(Agency).where(Agency.active == True)
    ).one()
    total = count_result[0] if isinstance(count_result, tuple) else count_result
    agencies = session.exec(stmt.offset(offset).limit(limit)).all()

    result: list[tuple[Agency, User | None]] = []
    for agency in agencies:
        user = session.exec(
            select(User).where(User.agency_id == agency.id, User.role == UserRole.AGENCY)
        ).first()
        result.append((agency, user))

    return result, total


def get_agency_by_id(session: Session, agency_id: int) -> tuple[Agency, User | None] | None:
    """
    Obtiene una agencia activa por ID con su usuario principal.
    Retorna None si no existe o está inactiva.
    """
    agency = session.get(Agency, agency_id)
    if agency is None or not agency.active:
        return None
    user = session.exec(
        select(User).where(User.agency_id == agency.id, User.role == UserRole.AGENCY)
    ).first()
    return agency, user


def update_agency(session: Session, agency_id: int, data: AgencyUpdate) -> Agency | None:
    """Actualiza una agencia. Retorna None si no existe o está inactiva."""
    agency = session.get(Agency, agency_id)
    if agency is None or not agency.active:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(agency, key, value)

    session.add(agency)
    session.commit()
    session.refresh(agency)
    return agency


def delete_agency_logical(session: Session, agency_id: int) -> bool:
    """
    Borrado lógico: desactiva la agencia y todos los usuarios/vendedores asociados.
    Retorna True si se desactivó, False si no existe o ya estaba inactiva.
    """
    agency = session.get(Agency, agency_id)
    if agency is None or not agency.active:
        return False

    agency.active = False

    # Desactivar usuarios de la agencia (admins con agency_id = agency_id)
    users_agency = session.exec(select(User).where(User.agency_id == agency_id)).all()
    for user in users_agency:
        user.active = False
        session.add(user)

    # Desactivar vendedores de la agencia y sus usuarios
    sellers = session.exec(select(Seller).where(Seller.agency_id == agency_id)).all()
    for seller in sellers:
        seller.active = False
        session.add(seller)
        # Desactivar el User del seller
        seller_user = session.get(User, seller.user_id)
        if seller_user:
            seller_user.active = False
            session.add(seller_user)

    session.add(agency)
    session.commit()
    return True
