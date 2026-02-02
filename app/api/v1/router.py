from fastapi import APIRouter
from app.api.v1 import quotes

router = APIRouter(prefix="/v1")

router.include_router(quotes.router, prefix="/quotes", tags=["Quotes"])
