from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raíz del repo: app/core/config.py -> ../../..
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


# Mapeo: destination_id (frontend) → Cardinal destinoId.
# 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Cotizador API"
    environment: str = "development"
    # Lista separada por comas de orígenes permitidos por CORS.
    # Ejemplo: "https://mi-frontend.com,https://staging-mi-frontend.com"
    # Para permitir todos: "*"
    cors_allow_origins: str = "*"
    # Sobrescribir con DATABASE_URL en .env según el entorno
    database_url: str = "sqlite:///./cotizador.db"
    # JWT - generar clave aleatoria: python -c "import secrets; print(secrets.token_hex(32))"
    secret_key: str = "dev-secret-cambiar-en-produccion"
    jwt_expire_minutes: int = 1440  # 24 horas
    # Fernet (32 bytes url-safe base64): python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Vacío = no se guarda contraseña recuperable para el admin.
    password_encryption_key: str = Field(
        default="",
        validation_alias="PASSWORD_ENCRYPTION_KEY",
    )

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

    # Terrawind (SETW)
    terrawind_base_url: str = "https://sandbox.setw.net/emision/api/v2/"
    terrawind_user: str = ""
    terrawind_password: str = ""

    # Universal Assistance (Siebel SOAP)
    universal_soap_url: str = (
        "https://siebelqa.universal-assistance.com:8443/siebel/app/eai_anon/esn"
        "?SWEExtSource=SecureWebService&SWEExtCmd=Execute"
    )
    universal_username: str = ""
    universal_password: str = ""
    universal_organizacion_emisora: str = ""
    universal_convenio: str = ""
    universal_folleto: str = ""
    # Mapeos del orquestador (1-5 + trip_type) a los valores esperados por Universal.
    # Se dejan vacíos para no asumir catálogos hasta definirlos con listas oficiales.
    universal_pais_origen_ar: str = "ARGENTINA"
    universal_destino_nacional: str = "Territorio Nacional"
    universal_destino_latinoamerica: str = "América del Sur (salvo Vzla)"
    universal_destino_europa: str = "Europa"
    universal_destino_resto_mundo: str = "Internacional Mundo"
    universal_destino_norteamerica: str = "America del norte"
    universal_tipo_viaje_unico_viaje: str = "Un viaje"
    universal_tipo_viaje_multiviaje: str = "Varios viajes"
    universal_tipo_viaje_larga_estadia: str = ""


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


def get_universal_destinos(settings: Settings) -> dict[int, str]:
    """destination_id (1-5) -> Destino SOAP Universal, solo valores configurados."""
    mapping: dict[int, str] = {}
    if settings.universal_destino_nacional:
        mapping[1] = settings.universal_destino_nacional
    if settings.universal_destino_latinoamerica:
        mapping[2] = settings.universal_destino_latinoamerica
    if settings.universal_destino_europa:
        mapping[3] = settings.universal_destino_europa
    if settings.universal_destino_resto_mundo:
        mapping[4] = settings.universal_destino_resto_mundo
    if settings.universal_destino_norteamerica:
        mapping[5] = settings.universal_destino_norteamerica
    return mapping


def get_universal_trip_types(settings: Settings) -> dict[str, str]:
    """trip_type interno -> TipoViaje SOAP Universal, solo valores configurados."""
    mapping: dict[str, str] = {}
    if settings.universal_tipo_viaje_unico_viaje:
        mapping["unico_viaje"] = settings.universal_tipo_viaje_unico_viaje
    if settings.universal_tipo_viaje_multiviaje:
        mapping["multiviaje"] = settings.universal_tipo_viaje_multiviaje
    if settings.universal_tipo_viaje_larga_estadia:
        mapping["larga_estadia"] = settings.universal_tipo_viaje_larga_estadia
    return mapping


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
