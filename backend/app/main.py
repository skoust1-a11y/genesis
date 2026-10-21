from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import audit, auth, journal, live, market, notifications
from app.api.routes import router
from app.audit.middleware import AuditContextMiddleware
from app.config import get_settings
from app.logger import RequestLoggingMiddleware, configure_logging

load_dotenv()

settings = get_settings()
configure_logging(settings)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if settings.token_hashes and "*" in origins:
    raise RuntimeError("CORS_ORIGINS='*' is refused when API_TOKENS is set: list the frontend origin explicitly")
app = FastAPI(title="Trading Strategy Lab API", version="0.1.0")
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(AuditContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$" if not settings.token_hashes else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.include_router(router)
app.include_router(journal.router)
app.include_router(live.router)
app.include_router(audit.router)
app.include_router(market.router)
app.include_router(auth.router)
app.include_router(notifications.router)
