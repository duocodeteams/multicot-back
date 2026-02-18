from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Cotizador API"
    environment: str = "development"
    # Sobrescribir con DATABASE_URL en .env según el entorno
    database_url: str = "sqlite:///./cotizador.db"
    # JWT - generar clave aleatoria: python -c "import secrets; print(secrets.token_hex(32))"
    secret_key: str = "dev-secret-cambiar-en-produccion"
    jwt_expire_minutes: int = 1440  # 24 horas

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
