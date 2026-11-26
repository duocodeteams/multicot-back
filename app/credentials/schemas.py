from pydantic import BaseModel, Field


class CardinalCredentials(BaseModel):
    """Identidad de Cardinal ya resuelta. El guid vacío significa que no hay cuenta."""

    agente_emisor_guid: str = ""
    agente_emisor_secreto: str = ""


class CardinalCredentialWrite(BaseModel):
    agente_emisor_guid: str = Field(min_length=1)
    agente_emisor_secreto: str = ""


class UniversalCredentialWrite(BaseModel):
    """RowId de Siebel. Usuario y convenio siguen siendo los del servidor."""

    organizacion_emisora: str = Field(min_length=1, max_length=40)


CREDENTIAL_WRITES: dict[str, type[BaseModel]] = {
    "cardinal": CardinalCredentialWrite,
    "universal": UniversalCredentialWrite,
}


class AgencyCredentialResponse(BaseModel):
    company_slug: str
    company_name: str
    uses_default: bool
    credentials: dict[str, str] | None = None


class AgencyCredentialListResponse(BaseModel):
    items: list[AgencyCredentialResponse]
