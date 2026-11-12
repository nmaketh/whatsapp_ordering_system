import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_engine, get_settings_from_request, get_store
from app.core.security import valid_meta_signature
from app.db.models import Merchant
from app.db.session import get_db, set_tenant
from app.services.fsm import ConversationEngine
from app.services.meta_webhook import (
    merchant_id_from_payment_reference,
    normalise_payment_callback,
    parse_meta_messages,
)
from app.services.redis_store import RedisStore

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def enqueue_messages(store: RedisStore, phone_number_id: str, messages: list) -> None:
    for message in messages:
        await store.publish_outbound(phone_number_id, message.recipient, message.payload)


@router.get("/whatsapp")
async def verify_whatsapp_webhook(request: Request) -> Response:
    settings = get_settings_from_request(request)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = settings.meta_verify_token.get_secret_value()
    if (
        mode == "subscribe"
        and token
        and hmac.compare_digest(token, expected)
        and challenge is not None
    ):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification failed")


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    request: Request, store: RedisStore = Depends(get_store)
) -> dict[str, bool]:
    settings = get_settings_from_request(request)
    raw_body = await request.body()
    if not valid_meta_signature(
        raw_body,
        request.headers.get("X-Hub-Signature-256"),
        settings.meta_app_secret.get_secret_value(),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Meta webhook signature"
        )
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    # The HTTP endpoint does only authentication, deduplication and a stream append.
    # All database/provider work happens after this acknowledgement in the worker.
    for event in parse_meta_messages(payload):
        if await store.claim_message(event.message_id):
            await store.publish_inbound(
                {
                    "message_id": event.message_id,
                    "phone_number_id": event.phone_number_id,
                    "wa_id": event.wa_id,
                    "display_name": event.display_name,
                    "text": event.text,
                    "action_id": event.action_id,
                    "location": event.location,
                    "raw": event.raw,
                }
            )
    return {"accepted": True}


@router.post("/momo", status_code=status.HTTP_200_OK)
async def receive_payment_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    store: RedisStore = Depends(get_store),
    engine: ConversationEngine = Depends(get_engine),
) -> dict[str, bool]:
    settings = get_settings_from_request(request)
    expected = (
        settings.payment_callback_secret.get_secret_value()
        if settings.payment_callback_secret
        else None
    )
    actual = request.headers.get("X-KaziFlow-Callback-Secret")
    if settings.is_production and not expected:
        raise HTTPException(status_code=503, detail="payment callback secret is not configured")
    if expected and not (actual and hmac.compare_digest(expected, actual)):
        raise HTTPException(status_code=401, detail="invalid payment callback secret")
    try:
        payload: dict[str, Any] = await request.json()
        merchant_reference, successful, provider_reference = normalise_payment_callback(payload)
        merchant_id = merchant_id_from_payment_reference(merchant_reference)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    merchant = await db.scalar(
        select(Merchant).where(Merchant.id == merchant_id, Merchant.active.is_(True))
    )
    if not merchant:
        return {"accepted": True}
    await set_tenant(db, merchant.id)
    order = await engine.payments.settle_callback(
        db, merchant_reference, successful, provider_reference, payload
    )
    if not order:
        # Return 200 to avoid a provider retry storm for unknown/expired references.
        return {"accepted": True}
    messages = await engine.dispatch_paid_order(db, merchant, order) if successful else []
    await db.commit()
    await enqueue_messages(store, merchant.waba_phone_number_id, messages)
    return {"accepted": True}
