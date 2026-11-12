import hmac

from fastapi import Header, HTTPException, Request, status

from app.core.config import Settings
from app.services.fsm import ConversationEngine
from app.services.redis_store import RedisStore


def get_store(request: Request) -> RedisStore:
    return request.app.state.store


def get_engine(request: Request) -> ConversationEngine:
    return request.app.state.conversation_engine


def get_settings_from_request(request: Request) -> Settings:
    return request.app.state.settings


async def require_admin(
    request: Request,
    x_kaziflow_admin_key: str | None = Header(default=None),
) -> None:
    settings = get_settings_from_request(request)
    expected = settings.admin_api_key.get_secret_value() if settings.admin_api_key else None
    if not expected and settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="admin API is not configured"
        )
    if expected and not (
        x_kaziflow_admin_key and hmac.compare_digest(expected, x_kaziflow_admin_key)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin API key"
        )
