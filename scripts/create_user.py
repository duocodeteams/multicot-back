"""Seed inicial: crea un admin.

Ejecutar:
  poetry run python scripts/create_user.py
"""
import sys
from pathlib import Path

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.core.database import engine, create_db_and_tables
from app.core.retrievable_password import encrypt_for_storage
from app.core.security import hash_password
from app.models import User
from app.models.user import UserRole


def main():
    create_db_and_tables()
    with Session(engine) as session:
        admin_email = "asosa@biant.com.ar"
        admin_password = "Frescoazul18"  # <- reemplazá por la contraseña real

        # Verificar si ya existe el admin
        existing_admin = session.exec(select(User).where(User.email == admin_email)).first()
        if not existing_admin:
            admin = User(
                email=admin_email,
                password_hash=hash_password(admin_password),
                password_encrypted=encrypt_for_storage(admin_password),
                role=UserRole.ADMIN,
            )
            session.add(admin)
            session.commit()
            print("Admin creado:")
            print(f"  - {admin_email} (role: admin)")
        else:
            print("Admin ya existe. No se creó nada.")


if __name__ == "__main__":
    main()
