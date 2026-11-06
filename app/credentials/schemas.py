from pydantic import BaseModel, Field


class CardinalCredentials(BaseModel):
    """Identidad de Cardinal ya resuelta. El guid vacío significa que no hay cuenta."""

    agente_emisor_guid: str = ""
    agente_emisor_secreto: str = ""


class CardinalCredentialWrite(BaseModel):
    agente_emisor_guid: str = Field(min_length=1)
    agente_emisor_secreto: str = ""


CREDENTIAL_WRITES: dict[str, type[BaseModel]] = {
    "cardinal": CardinalCredentialWrite,
}


class AgencyCredentialResponse(BaseModel):
    company_slug: str
    company_name: str
    uses_default: bool
    credentials: dict[str, str] | None = None


class AgencyCredentialListResponse(BaseModel):
    items: list[AgencyCredentialResponse]
