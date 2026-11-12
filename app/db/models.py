import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ItemType(str, enum.Enum):
    PRODUCT = "PRODUCT"
    SERVICE_BOOKING = "SERVICE_BOOKING"


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    AWAITING_LOCATION = "AWAITING_LOCATION"
    AWAITING_LANDMARK = "AWAITING_LANDMARK"
    AWAITING_PAYMENT_METHOD = "AWAITING_PAYMENT_METHOD"
    AWAITING_PAYMENT_CALLBACK = "AWAITING_PAYMENT_CALLBACK"
    AWAITING_PAYMENT_RECOVERY = "AWAITING_PAYMENT_RECOVERY"
    PAYMENT_PENDING_VERIFICATION = "PAYMENT_PENDING_VERIFICATION"
    PAID = "PAID"
    PREPARING = "PREPARING"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class FulfillmentType(str, enum.Enum):
    DELIVERY = "DELIVERY"
    PICKUP = "PICKUP"
    DINE_IN = "DINE_IN"
    BOOKING = "BOOKING"


class PaymentProvider(str, enum.Enum):
    MTN_MOMO = "MTN_MOMO"
    AIRTEL_MONEY = "AIRTEL_MONEY"
    MPESA = "MPESA"
    MGURUSH = "MGURUSH"
    MANUAL_TILL = "MANUAL_TILL"
    CASH_ON_DELIVERY = "CASH_ON_DELIVERY"


class PaymentStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    CLAIMED = "CLAIMED"
    REJECTED = "REJECTED"


class Merchant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchants"

    business_name: Mapped[str] = mapped_column(String(160), nullable=False)
    waba_phone_number_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="UGX")
    usd_exchange_rate: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False, default=1)
    rounding_increment: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    manual_payment_details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dispatch_channel_wa_id: Mapped[str | None] = mapped_column(String(64))
    shop_latitude: Mapped[float | None] = mapped_column(Numeric(10, 7))
    shop_longitude: Mapped[float | None] = mapped_column(Numeric(10, 7))
    delivery_radius_km: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=7)
    delivery_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    items: Mapped[list["Item"]] = relationship(back_populates="merchant")
    orders: Mapped[list["Order"]] = relationship(back_populates="merchant")


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "items"
    __table_args__ = (Index("ix_items_merchant_category", "merchant_id", "category"),)

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="General")
    base_price_usd: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    item_type: Mapped[ItemType] = mapped_column(
        Enum(ItemType), nullable=False, default=ItemType.PRODUCT
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    deposit_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    merchant: Mapped[Merchant] = relationship(back_populates="items")


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("merchant_id", "wa_id_hash"),)

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    wa_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    wa_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("merchant_id", "reference"),
        Index("ix_orders_merchant_status", "merchant_id", "status"),
        Index("ix_orders_merchant_customer", "merchant_id", "customer_wa_id_hash"),
    )

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    customer_wa_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_wa_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), nullable=False, default=OrderStatus.DRAFT
    )
    fulfillment_type: Mapped[FulfillmentType | None] = mapped_column(Enum(FulfillmentType))
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate_locked: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    subtotal_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    location_encrypted: Mapped[str | None] = mapped_column(Text)
    landmark_encrypted: Mapped[str | None] = mapped_column(Text)
    booking_slot_id: Mapped[str | None] = mapped_column(
        ForeignKey("booking_slots.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    merchant: Mapped[Merchant] = relationship(back_populates="orders")
    lines: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list["PaymentLedger"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "order_items"

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    title_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="lines")


class BookingSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "booking_slots"
    __table_args__ = (
        UniqueConstraint("merchant_id", "staff_id", "start_time"),
        Index("ix_booking_slots_availability", "merchant_id", "start_time", "is_booked"),
    )

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    service_item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    staff_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_booked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    booked_by_order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )


class PaymentLedger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payment_ledgers"
    __table_args__ = (
        UniqueConstraint("merchant_id", "merchant_reference"),
        Index("ix_payment_ledgers_provider_reference", "provider", "provider_reference"),
    )

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[PaymentProvider] = mapped_column(Enum(PaymentProvider), nullable=False)
    merchant_reference: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(120))
    claimed_transaction_id: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), nullable=False)
    raw_callback: Mapped[dict | None] = mapped_column(JSON)

    order: Mapped[Order] = relationship(back_populates="payments")


class AuditEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_merchant_created", "merchant_id", "created_at"),)

    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    actor_wa_id_encrypted: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
