from fastapi import FastAPI
from app.api.v1.router import router

app = FastAPI(
    title="Cotizador API",
    version="0.1.0"
)

app.include_router(router)

@app.get("/health")
def health_check():
    return {"status": "ok"}
