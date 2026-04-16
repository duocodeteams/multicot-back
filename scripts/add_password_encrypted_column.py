"""Ejecutar una vez si la tabla users ya existía sin la columna password_encrypted.

    poetry run python scripts/add_password_encrypted_column.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import engine


def main() -> None:
    with engine.connect() as conn:
        if "sqlite" in str(engine.url):
            rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
            cols = {r[1] for r in rows}
            if "password_encrypted" in cols:
                print("Columna password_encrypted ya existe.")
                return
            conn.execute(text("ALTER TABLE users ADD COLUMN password_encrypted VARCHAR"))
            conn.commit()
            print("Columna password_encrypted agregada.")
        else:
            print("Para PostgreSQL u otros, ejecutá un ALTER equivalente a mano o con Alembic.")


if __name__ == "__main__":
    main()
