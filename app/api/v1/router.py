from fastapi import APIRouter

from app.api.v1 import agencies, auth, quotes, sellers

router = APIRouter(prefix="/v1")

router.include_router(auth.router, prefix="/auth", tags=["Auth"])
router.include_router(agencies.router, prefix="/agencies", tags=["Agencies"])
router.include_router(sellers.router, prefix="/sellers", tags=["Sellers"])
router.include_router(quotes.router, prefix="/quotes", tags=["Quotes"])
