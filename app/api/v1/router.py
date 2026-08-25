from fastapi import APIRouter

from app.api.v1 import agencies, auth, companies, plans, quotes, sellers, users

router = APIRouter(prefix="/v1")

router.include_router(auth.router, prefix="/auth", tags=["Auth"])
router.include_router(agencies.router, prefix="/agencies", tags=["Agencies"])
router.include_router(sellers.router, prefix="/sellers", tags=["Sellers"])
router.include_router(users.router, prefix="/users", tags=["Users"])
router.include_router(companies.router, prefix="/companies", tags=["Companies"])
router.include_router(plans.router, prefix="/plans", tags=["Plans"])
router.include_router(quotes.router, prefix="/quotes", tags=["Quotes"])
