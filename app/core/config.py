from functools import lru_cache

from pydantic_settings import BaseSettings


# Mapeo: destination_id (frontend) → Cardinal destinoId.
# 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica.


class Settings(BaseSettings):
    app_name: str = "Cotizador API"
    environment: str = "development"
    # Sobrescribir con DATABASE_URL en .env según el entorno
    database_url: str = "sqlite:///./cotizador.db"
    # JWT - generar clave aleatoria: python -c "import secrets; print(secrets.token_hex(32))"
    secret_key: str = "dev-secret-cambiar-en-produccion"
    jwt_expire_minutes: int = 1440  # 24 horas

    # Cardinal Assistance (Evoucher 2)
    cardinal_base_url: str = "https://ev2.bluesoft.com.ar/webservice"
    cardinal_agente_emisor_guid: str = ""
    cardinal_agente_emisor_secreto: str = ""
    # IDs de destino Cardinal (obtener de POST /parametros con parametros=destinos).
    # Origen AR se resuelve por nombre "Argentina" vía /parametros.
    cardinal_destino_nacional_id: int | None = None
    cardinal_destino_europa_id: int | None = None
    cardinal_destino_latinoamerica_id: int | None = None
    cardinal_destino_resto_mundo_id: int | None = None
    cardinal_destino_norteamerica_id: int | None = None

    # Email usado cuando las APIs de cotización lo exigen (Cardinal, GoAssistance, etc.)
    cotizador_default_email: str = "cotizador@cotizador.local"

    # GoAssistance (ATV - Asegura Tu Viaje)
    go_assistance_base_url: str = "https://sistema.aseguratuviaje.com/webapi18"
    go_assistance_webservice: str = ""

    # Pax Assistance
    pax_base_url: str = "https://www.stg.paxassistance.com"
    pax_api_key: str = ""

    # New Travel
    new_travel_base_url: str = "https://cacao.ilstechnik.com/api/v1"
    new_travel_user: str = ""
    new_travel_password: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


def get_cardinal_destino_ids(settings: Settings) -> dict[int, int]:
    """Diccionario destination_id (1-5) → Cardinal destinoId. Solo incluye los configurados."""
    mapping: dict[int, int] = {}
    # 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica
    if settings.cardinal_destino_nacional_id is not None:
        mapping[1] = settings.cardinal_destino_nacional_id
    if settings.cardinal_destino_latinoamerica_id is not None:
        mapping[2] = settings.cardinal_destino_latinoamerica_id
    if settings.cardinal_destino_europa_id is not None:
        mapping[3] = settings.cardinal_destino_europa_id
    if settings.cardinal_destino_resto_mundo_id is not None:
        mapping[4] = settings.cardinal_destino_resto_mundo_id
    if settings.cardinal_destino_norteamerica_id is not None:
        mapping[5] = settings.cardinal_destino_norteamerica_id
    return mapping


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
