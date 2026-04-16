import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import router
from app.core.config import settings
from app.core.database import create_db_and_tables

# En desarrollo, mostrar todos los logs (DEBUG) para depuración.
if settings.environment == "development":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # En producción el esquema lo define Alembic; create_all aquí chocaría con las migraciones (ENUMs duplicados).
    if settings.environment == "development":
        create_db_and_tables()
    yield


def _parse_cors_origins(raw_origins: str) -> list[str]:
    value = raw_origins.strip()
    if not value:
        return []
    if value == "*":
        return ["*"]
    return [origin.strip() for origin in value.split(",") if origin.strip()]


app = FastAPI(
    title="Cotizador API",
    version="0.1.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

cors_origins = _parse_cors_origins(settings.cors_allow_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    # Con wildcard no se pueden usar credenciales por política CORS del navegador.
    allow_credentials=(cors_origins != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
