"""Carga masiva de planes desde un CSV (una vez en dev, otra en prod).

El CSV se exporta de Sheets. Markup y destinos quedan en 0 / apagados;
el admin los define después desde el panel.

  poetry run python scripts/load_plans.py ruta/al/planes.csv
  poetry run python scripts/load_plans.py ruta/al/planes.csv --dry-run
  poetry run python scripts/load_plans.py ruta/al/planes.csv --delimiter ';'
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.companies.seed import COMPANY_SEED
from app.core.database import engine
from app.models.company import Company
from app.plans.schemas import PlanCreate
from app.plans.service import create_plan

VALID_SLUGS = {slug for slug, _name, _active in COMPANY_SEED}
REQUIRED_COLUMNS = ("company_slug", "external_plan_id", "name")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Carga planes desde CSV")
    parser.add_argument("csv_path", type=Path, help="Ruta al CSV exportado de Sheets")
    parser.add_argument(
        "--delimiter",
        default=",",
        help="Separador del CSV. Sheets en español suele usar ';'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e imprime lo que se cargaría, sin escribir en la DB",
    )
    return parser.parse_args()


def _read_rows(path: Path, delimiter: str) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError("El CSV no tiene encabezado")
        columns = {name.strip().lower() for name in reader.fieldnames if name}
        missing = [col for col in REQUIRED_COLUMNS if col not in columns]
        if missing:
            raise ValueError(
                f"Faltan columnas {missing}. Encabezado esperado: "
                "company_slug,external_plan_id,name"
            )
        rows: list[dict[str, str]] = []
        for raw in reader:
            normalized = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw.items()
            }
            if not any(normalized.values()):
                continue
            rows.append(normalized)
        return rows


def _load_companies(session: Session) -> dict[str, Company]:
    companies = session.exec(select(Company)).all()
    return {company.slug: company for company in companies}


def main() -> int:
    args = _parse_args()
    if not args.csv_path.is_file():
        print(f"No existe el archivo: {args.csv_path}", file=sys.stderr)
        return 1

    try:
        rows = _read_rows(args.csv_path, args.delimiter)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    created = 0
    skipped = 0
    errors = 0

    with Session(engine) as session:
        companies = _load_companies(session)
        for index, row in enumerate(rows, start=2):
            line_label = f"fila {index}"
            slug = row.get("company_slug", "").lower()
            external_plan_id = row.get("external_plan_id", "")
            name = row.get("name", "")
            try:
                if slug not in VALID_SLUGS:
                    raise ValueError(
                        f"company_slug desconocido: {slug!r}. "
                        f"Válidos: {', '.join(sorted(VALID_SLUGS))}"
                    )
                company = companies.get(slug)
                if company is None or company.id is None:
                    raise ValueError(
                        f"no hay compañía '{slug}' en la DB. "
                        "Levantá la API una vez para que corra el seed."
                    )
                if not external_plan_id:
                    raise ValueError("external_plan_id vacío")
                if not name:
                    raise ValueError("name vacío")
                payload = PlanCreate(
                    company_id=company.id,
                    external_plan_id=external_plan_id,
                    name=name,
                )
                if args.dry_run:
                    print(f"{line_label}: {slug} {external_plan_id} {name!r}")
                    created += 1
                    continue
                create_plan(session, payload)
                print(f"{line_label}: creado {slug} {external_plan_id}")
                created += 1
            except ValueError as exc:
                message = str(exc)
                if "Ya existe un plan" in message:
                    print(f"{line_label}: omitido ({message})")
                    skipped += 1
                else:
                    print(f"{line_label}: error ({message})", file=sys.stderr)
                    errors += 1

    mode = "dry-run" if args.dry_run else "carga"
    print(
        f"{mode}: {created} ok, {skipped} ya existían, {errors} con error, "
        f"{len(rows)} filas"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
