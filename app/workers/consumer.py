import asyncio
import logging
from contextlib import suppress
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import PiiCipher
from app.db.models import Merchant
from app.db.session import SessionLocal, set_tenant
from app.services.fsm import ConversationEngine, InboundChat
from app.services.redis_store import INBOUND_STREAM, OUTBOUND_STREAM, RedisStore
from app.services.whatsapp import OutboundMessage, WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
INBOUND_GROUP = "kaziflow-inbound-workers"
OUTBOUND_GROUP = "kaziflow-outbound-workers"


class InboundConsumer:
    def __init__(self, store: RedisStore, engine: ConversationEngine) -> None:
        self.store = store
        self.engine = engine

    async def run(self, consumer_name: str) -> None:
        await self.store.ensure_consumer_group(INBOUND_GROUP)
        logger.info("inbound worker %s started", consumer_name)
        while True:
            records = await self.store.client.xreadgroup(
                INBOUND_GROUP, consumer_name, {INBOUND_STREAM: ">"}, count=1, block=5000
            )
            for _, entries in records:
                for stream_id, fields in entries:
                    await self._process(stream_id, fields)

    async def _process(self, stream_id: str, fields: dict[str, str]) -> None:
        try:
            payload: dict[str, Any] = self.store.unpack(fields["payload"])
            async with SessionLocal() as db:
                merchant = await db.scalar(
                    select(Merchant).where(
                        Merchant.waba_phone_number_id == payload["phone_number_id"],
                        Merchant.active.is_(True),
                    )
                )
                if not merchant:
                    await self.store.publish_dead_letter(payload, "unknown_or_inactive_merchant")
                    await self.store.client.xack(INBOUND_STREAM, INBOUND_GROUP, stream_id)
                    return
                await set_tenant(db, merchant.id)
                # One stream message is processed to completion before the next. For horizontal
                # deployments, run a single consumer per hash shard; the customer lock protects
                # against accidental overlapping consumers during a rollout.
                if not await self.store.acquire_customer_lock(merchant.id, payload["wa_id"]):
                    await self.store.publish_dead_letter(payload, "customer_processing_lock_busy")
                    await self.store.client.xack(INBOUND_STREAM, INBOUND_GROUP, stream_id)
                    return
                try:
                    messages = await self.engine.handle(
                        db,
                        merchant,
                        InboundChat(
                            wa_id=payload["wa_id"],
                            display_name=payload.get("display_name"),
                            text=payload.get("text"),
                            action_id=payload.get("action_id"),
                            location=payload.get("location"),
                        ),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
                finally:
                    await self.store.release_customer_lock(merchant.id, payload["wa_id"])
                for message in messages:
                    await self.store.publish_outbound(
                        merchant.waba_phone_number_id, message.recipient, message.payload
                    )
            await self.store.client.xack(INBOUND_STREAM, INBOUND_GROUP, stream_id)
        except Exception as exc:
            logger.exception("inbound stream event %s failed", stream_id)
            # Acknowledge malformed/failing events after retaining their full payload in the DLQ.
            with suppress(Exception):
                await self.store.publish_dead_letter(
                    {"fields": fields}, f"worker_error:{type(exc).__name__}"
                )
                await self.store.client.xack(INBOUND_STREAM, INBOUND_GROUP, stream_id)


class OutboundConsumer:
    def __init__(self, store: RedisStore, client: WhatsAppClient) -> None:
        self.store = store
        self.client = client

    async def run(self, consumer_name: str) -> None:
        await self.store.ensure_outbound_consumer_group(OUTBOUND_GROUP)
        logger.info("outbound worker %s started", consumer_name)
        while True:
            records = await self.store.client.xreadgroup(
                OUTBOUND_GROUP, consumer_name, {OUTBOUND_STREAM: ">"}, count=10, block=5000
            )
            for _, entries in records:
                for stream_id, fields in entries:
                    try:
                        job = self.store.unpack(fields["payload"])
                        message = OutboundMessage(job["recipient"], job["message"])
                        await self.client.send(job["phone_number_id"], message)
                        await self.store.client.xack(OUTBOUND_STREAM, OUTBOUND_GROUP, stream_id)
                    except Exception:
                        logger.exception("outbound stream event %s failed", stream_id)
                        # It remains pending for recovery via XAUTOCLAIM rather than being lost.


async def main() -> None:
    settings = get_settings()
    store = RedisStore.from_settings(settings)
    cipher = PiiCipher(
        settings.pii_encryption_key.get_secret_value() if settings.pii_encryption_key else None
    )
    engine = ConversationEngine(store, cipher, settings.session_ttl_seconds)
    client = WhatsAppClient(settings, store)
    try:
        await asyncio.gather(
            InboundConsumer(store, engine).run(settings.worker_consumer_name),
            OutboundConsumer(store, client).run(settings.worker_consumer_name),
        )
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
