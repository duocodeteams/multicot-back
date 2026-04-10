"""Script para crear un usuario de prueba. Ejecutar: poetry run python scripts/create_user.py"""
import sys
from datetime import date
from pathlib import Path

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from decimal import Decimal

from app.core.database import engine, create_db_and_tables
from app.core.retrievable_password import encrypt_for_storage
from app.core.security import hash_password
from app.models import Agency, Seller, User
from app.models.agency import BillingFrequency, PaymentMethod, TaxCondition
from app.models.user import UserRole


def main():
    create_db_and_tables()
    with Session(engine) as session:
        # Verificar si ya existe el admin
        existing_admin = session.exec(select(User).where(User.email == "admin@test.com")).first()
        if not existing_admin:
            admin = User(
                email="admin@test.com",
                password_hash=hash_password("password123"),
                password_encrypted=encrypt_for_storage("password123"),
                role=UserRole.ADMIN,
            )
            session.add(admin)
            session.commit()
            print("Admin creado:")
            print("  - admin@test.com / password123 (role: admin)")

        # Verificar si ya existen usuarios de prueba
        existing = session.exec(select(User).where(User.email == "agencia@test.com")).first()
        if existing:
            print("Usuarios de prueba ya existen.")
            return

        # Crear agencia de prueba con todos los campos requeridos
        agency = Agency(
            name="Agencia Test",
            legal_name="Agencia Test S.A.",
            tax_id="20-12345678-9",
            address="Calle Falsa 123",
            country="AR",
            legal_representative_name="Juan Pérez",
            agency_email="agencia@test.com",
            office_phone="+54 11 1234-5678",
            activation_date=date.today(),
            billing_frequency=BillingFrequency.MONTHLY,
            payment_method=PaymentMethod.TRANSFER,
            tax_condition=TaxCondition.RESPONSABLE_INSCRIPTO,
        )
        session.add(agency)
        session.commit()
        session.refresh(agency)

        # Usuario agencia
        user_agency = User(
            email="agencia@test.com",
            password_hash=hash_password("password123"),
            password_encrypted=encrypt_for_storage("password123"),
            role=UserRole.AGENCY,
            agency_id=agency.id,
        )
        session.add(user_agency)

        # Usuario vendedor independiente + perfil Seller
        user_seller = User(
            email="vendedor@test.com",
            password_hash=hash_password("password123"),
            password_encrypted=encrypt_for_storage("password123"),
            role=UserRole.SELLER,
        )
        session.add(user_seller)
        session.flush()

        seller = Seller(
            user_id=user_seller.id,
            agency_id=None,  # Independiente
            first_name="Carlos",
            last_name="Vendedor",
            address="Av. Test 456",
            nationality="AR",
            birth_date=date(1990, 5, 15),
            comments="Vendedor de prueba",
            commission=Decimal("10"),
        )
        session.add(seller)
        session.commit()

        print("Usuarios creados:")
        print("  - agencia@test.com / password123 (role: agency)")
        print("  - vendedor@test.com / password123 (role: seller)")


if __name__ == "__main__":
    main()
