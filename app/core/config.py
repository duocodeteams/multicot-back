from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Cotizador API"
    environment: str = "development"


settings = Settings()
