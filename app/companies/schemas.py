from pydantic import BaseModel


class CompanyResponse(BaseModel):
    id: int
    slug: str
    name: str
    active: bool

    model_config = {"from_attributes": True}


class CompanyListResponse(BaseModel):
    items: list[CompanyResponse]
    total: int
    limit: int
    offset: int


class CompanyUpdate(BaseModel):
    active: bool
