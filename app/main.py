import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
    create_db_and_tables()
    yield


app = FastAPI(
    title="Cotizador API",
    version="0.1.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

app.include_router(router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
