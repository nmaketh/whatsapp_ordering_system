"""Low-frequency recovery and privacy jobs.

Run this module as a dedicated worker in production (for example every minute
through a scheduler). It is safe to run repeatedly; every job is idempotent.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.security import PiiCipher
from app.db.models import Merchant, Order, OrderStatus
from app.db.session import SessionLocal, set_tenant
from app.services.redis_store import RedisStore
from app.services.whatsapp import text

logger = logging.getLogger(__name__)


async def remind_abandoned_carts(store: RedisStore) -> int:
    now = int(time.time())
    reminders = 0
    for merchant_id, wa_id, session in await store.iter_sessions():
        inactive = now - int(session.get("updated_at_epoch", now))
        if not 20 * 60 <= inactive < 21 * 60 or session.get("abandonment_reminded"):
            continue
        if not session.get("cart"):
            continue
        async with SessionLocal() as db:
            merchant = await db.get(Merchant, merchant_id)
            if not merchant:
                continue
            await store.publish_outbound(
                merchant.waba_phone_number_id,
                wa_id,
                text(
                    wa_id,
                    "You left items in your cart. Reply MENU to resume, or EMPTY CART to start over.",
                ).payload,
            )
        session["abandonment_reminded"] = True
        await store.save_session(
            merchant_id, wa_id, session, max(60, get_settings().session_ttl_seconds - inactive)
        )
        reminders += 1
    return reminders


async def recover_payments_and_scrub_pii(store: RedisStore) -> tuple[int, int]:
    recovered = 0
    scrubbed = 0
    cipher = PiiCipher(
        get_settings().pii_encryption_key.get_secret_value()
        if get_settings().pii_encryption_key
        else None
    )
    # Merchant lookup is explicitly unscoped; every subsequent tenant table
    # operation sets its RLS context inside a separate transaction.
    async with SessionLocal() as root_db:
        merchants = list(
            (await root_db.scalars(select(Merchant).where(Merchant.active.is_(True)))).all()
        )
    for merchant in merchants:
        async with SessionLocal() as db:
            await set_tenant(db, merchant.id)
            timeout_cutoff = datetime.now(UTC) - timedelta(seconds=90)
            pending = list(
                (
                    await db.scalars(
                        select(Order).where(
                            Order.merchant_id == merchant.id,
                            Order.status == OrderStatus.AWAITING_PAYMENT_CALLBACK,
                            Order.updated_at < timeout_cutoff,
                        )
                    )
                ).all()
            )
            for order in pending:
                order.status = OrderStatus.AWAITING_PAYMENT_RECOVERY
                recovered += 1
            pii_cutoff = datetime.now(UTC) - timedelta(days=7)
            result = await db.execute(
                update(Order)
                .where(
                    Order.merchant_id == merchant.id,
                    Order.status.in_([OrderStatus.COMPLETED, OrderStatus.CANCELLED]),
                    Order.updated_at < pii_cutoff,
                )
                .values(location_encrypted=None, landmark_encrypted=None)
            )
            scrubbed += result.rowcount or 0
            await db.commit()
            for order in pending:
                customer_wa_id = cipher.decrypt(order.customer_wa_id_encrypted)
                if not customer_wa_id:
                    continue
                await store.publish_outbound(
                    merchant.waba_phone_number_id,
                    customer_wa_id,
                    text(
                        customer_wa_id,
                        "We did not receive payment confirmation. Reply MENU to retry or choose Manual Till / Cash.",
                    ).payload,
                )
    return recovered, scrubbed


async def main() -> None:
    settings = get_settings()
    store = RedisStore.from_settings(settings)
    try:
        while True:
            reminders = await remind_abandoned_carts(store)
            recovered, scrubbed = await recover_payments_and_scrub_pii(store)
            if reminders or recovered or scrubbed:
                logger.info(
                    "maintenance: reminders=%s payment_recovery=%s pii_scrubbed=%s",
                    reminders,
                    recovered,
                    scrubbed,
                )
            await asyncio.sleep(60)
    finally:
        await store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
