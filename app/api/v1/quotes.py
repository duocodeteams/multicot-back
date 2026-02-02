from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def quote():
    return {"message": "Quote endpoint"}
