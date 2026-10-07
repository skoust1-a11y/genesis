from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import audit, journal, live
from app.api.routes import router
from app.audit.middleware import AuditContextMiddleware
from app.config import get_settings

load_dotenv()

settings = get_settings()
app = FastAPI(title="Trading Strategy Lab API", version="0.1.0")
app.add_middleware(AuditContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.include_router(router)
app.include_router(journal.router)
app.include_router(live.router)
app.include_router(audit.router)
