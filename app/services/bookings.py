from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookingSlot, Order
from app.services.redis_store import RedisStore


class SlotUnavailable(Exception):
    pass


class BookingService:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    async def available_slots(
        self, db: AsyncSession, merchant_id: str, service_id: str, start: datetime, end: datetime
    ) -> list[BookingSlot]:
        slots = list(
            (
                await db.scalars(
                    select(BookingSlot)
                    .where(
                        BookingSlot.merchant_id == merchant_id,
                        BookingSlot.service_item_id == service_id,
                        BookingSlot.start_time >= start,
                        BookingSlot.start_time < end,
                        BookingSlot.is_booked.is_(False),
                    )
                    .order_by(BookingSlot.start_time)
                )
            ).all()
        )
        available: list[BookingSlot] = []
        for slot in slots:
            locked = await self.store.is_slot_locked(
                merchant_id, service_id, slot.start_time.isoformat()
            )
            if not locked:
                available.append(slot)
        return available

    async def hold_slot(
        self, db: AsyncSession, merchant_id: str, service_id: str, slot_id: str, customer_wa_id: str
    ) -> BookingSlot:
        slot = await db.get(BookingSlot, slot_id)
        if (
            not slot
            or slot.is_booked
            or slot.merchant_id != merchant_id
            or slot.service_item_id != service_id
        ):
            raise SlotUnavailable()
        acquired = await self.store.acquire_slot_lock(
            slot.merchant_id, str(slot.service_item_id), slot.start_time.isoformat(), customer_wa_id
        )
        if not acquired:
            raise SlotUnavailable()
        return slot

    async def confirm_slot(
        self, db: AsyncSession, order: Order, customer_wa_id: str
    ) -> BookingSlot | None:
        if not order.booking_slot_id:
            return None
        slot = await db.get(BookingSlot, order.booking_slot_id, with_for_update=True)
        if not slot:
            raise SlotUnavailable()
        if slot.is_booked and slot.booked_by_order_id != order.id:
            raise SlotUnavailable()
        slot.is_booked = True
        slot.booked_by_order_id = order.id
        await self.store.release_slot_lock(
            slot.merchant_id,
            str(slot.service_item_id),
            slot.start_time.isoformat(),
            customer_wa_id,
        )
        return slot
