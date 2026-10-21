from fastapi import FastAPI

from app.api import amazon
from app.api.routes import health, router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Amazon India Seller Eligibility Checker",
        version="0.1.0",
        description=(
            "ASIN-level, evidence-backed listing eligibility for an authorised seller account. "
            "A brand-level result describes only the ASINs that were actually checked."
        ),
    )
    app.include_router(router)
    app.include_router(amazon.router)
    app.include_router(health)
    return app


app = create_app()
