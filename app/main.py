from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, webhooks
from app.core.config import get_settings
from app.core.security import PiiCipher
from app.db.session import close_db
from app.services.fsm import ConversationEngine
from app.services.redis_store import RedisStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    store = RedisStore.from_settings(settings)
    app.state.settings = settings
    app.state.store = store
    app.state.conversation_engine = ConversationEngine(
        store,
        PiiCipher(
            settings.pii_encryption_key.get_secret_value() if settings.pii_encryption_key else None
        ),
        settings.session_ttl_seconds,
    )
    try:
        yield
    finally:
        await store.close()
        await close_db()


app = FastAPI(
    title="KaziFlow Engine",
    version="0.1.0",
    description="WhatsApp-native ordering, booking and fulfilment engine.",
    lifespan=lifespan,
)
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/healthz", tags=["operations"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
