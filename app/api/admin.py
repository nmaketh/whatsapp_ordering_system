from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import BookingSlot, Item, ItemType, Merchant, Order
from app.db.session import get_db, set_tenant

router = APIRouter(prefix="/v1", tags=["merchant administration"])


class MerchantCreate(BaseModel):
    business_name: str = Field(min_length=2, max_length=160)
    waba_phone_number_id: str = Field(min_length=3, max_length=64)
    currency_code: str = Field(min_length=3, max_length=3)
    usd_exchange_rate: Decimal = Field(gt=0)
    rounding_increment: int = Field(default=1, ge=1)
    manual_payment_details: dict[str, Any] = Field(default_factory=dict)
    dispatch_channel_wa_id: str | None = None
    shop_latitude: Decimal | None = None
    shop_longitude: Decimal | None = None
    delivery_radius_km: Decimal = Field(default=7, gt=0)
    delivery_fee: int = Field(default=0, ge=0)


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1024)
    category: str = Field(default="General", min_length=1, max_length=60)
    base_price_usd: Decimal = Field(gt=0)
    item_type: ItemType = ItemType.PRODUCT
    duration_minutes: int | None = Field(default=None, ge=5, le=1440)
    deposit_percent: int = Field(default=0, ge=0, le=100)


class RateUpdate(BaseModel):
    usd_exchange_rate: Decimal = Field(gt=0)
    rounding_increment: int | None = Field(default=None, ge=1)


class SlotCreate(BaseModel):
    service_item_id: str
    staff_id: str = Field(default="default", max_length=64)
    start_time: datetime
    end_time: datetime


def merchant_view(merchant: Merchant) -> dict[str, Any]:
    return {
        "id": merchant.id,
        "business_name": merchant.business_name,
        "waba_phone_number_id": merchant.waba_phone_number_id,
        "currency_code": merchant.currency_code,
        "usd_exchange_rate": str(merchant.usd_exchange_rate),
        "rounding_increment": merchant.rounding_increment,
        "delivery_radius_km": str(merchant.delivery_radius_km),
    }


@router.post(
    "/merchants", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)]
)
async def create_merchant(
    payload: MerchantCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    existing = await db.scalar(
        select(Merchant).where(Merchant.waba_phone_number_id == payload.waba_phone_number_id)
    )
    if existing:
        raise HTTPException(
            status_code=409, detail="a merchant already uses this WhatsApp phone number ID"
        )
    merchant = Merchant(**payload.model_dump())
    db.add(merchant)
    await db.commit()
    await db.refresh(merchant)
    return merchant_view(merchant)


async def get_merchant_or_404(db: AsyncSession, merchant_id: str) -> Merchant:
    merchant = await db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="merchant not found")
    await set_tenant(db, merchant.id)
    return merchant


@router.post(
    "/merchants/{merchant_id}/items",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_item(
    merchant_id: str, payload: ItemCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await get_merchant_or_404(db, merchant_id)
    if payload.item_type == ItemType.SERVICE_BOOKING and not payload.duration_minutes:
        raise HTTPException(status_code=422, detail="booking services require duration_minutes")
    item = Item(merchant_id=merchant_id, **payload.model_dump())
    db.add(item)
    await db.commit()
    return {"id": item.id, "title": item.title, "type": item.item_type.value}


@router.put("/merchants/{merchant_id}/pricing", dependencies=[Depends(require_admin)])
async def update_rate(
    merchant_id: str, payload: RateUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    merchant = await get_merchant_or_404(db, merchant_id)
    merchant.usd_exchange_rate = payload.usd_exchange_rate
    if payload.rounding_increment is not None:
        merchant.rounding_increment = payload.rounding_increment
    await db.commit()
    return merchant_view(merchant)


@router.post(
    "/merchants/{merchant_id}/booking-slots",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_booking_slot(
    merchant_id: str, payload: SlotCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await get_merchant_or_404(db, merchant_id)
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must follow start_time")
    item = await db.scalar(
        select(Item).where(
            Item.id == payload.service_item_id,
            Item.merchant_id == merchant_id,
            Item.item_type == ItemType.SERVICE_BOOKING,
        )
    )
    if not item:
        raise HTTPException(
            status_code=422,
            detail="service_item_id must be an active booking service for this merchant",
        )
    slot = BookingSlot(merchant_id=merchant_id, **payload.model_dump())
    db.add(slot)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="conflicting staff slot") from exc
    return {"id": slot.id, "start_time": slot.start_time, "end_time": slot.end_time}


@router.get("/merchants/{merchant_id}/orders", dependencies=[Depends(require_admin)])
async def list_orders(merchant_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    await get_merchant_or_404(db, merchant_id)
    orders = list(
        (
            await db.scalars(
                select(Order)
                .where(Order.merchant_id == merchant_id)
                .order_by(Order.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    return [
        {
            "id": order.id,
            "reference": order.reference,
            "status": order.status.value,
            "total_amount": order.total_amount,
            "currency_code": order.currency_code,
            "fulfillment_type": order.fulfillment_type.value if order.fulfillment_type else None,
            "created_at": order.created_at,
        }
        for order in orders
    ]
