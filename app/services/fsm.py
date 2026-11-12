import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import PiiCipher
from app.db.models import (
    AuditEvent,
    Customer,
    FulfillmentType,
    Item,
    ItemType,
    Merchant,
    Order,
    OrderItem,
    OrderStatus,
    PaymentLedger,
    PaymentProvider,
)
from app.db.session import set_tenant
from app.services.bookings import BookingService, SlotUnavailable
from app.services.currency import local_price, money
from app.services.geo import validate_delivery_location
from app.services.payments import PaymentService
from app.services.redis_store import RedisStore
from app.services.whatsapp import OutboundMessage, buttons, list_message, text


@dataclass(frozen=True)
class InboundChat:
    wa_id: str
    display_name: str | None
    text: str | None = None
    action_id: str | None = None
    location: dict[str, Any] | None = None


class ConversationEngine:
    """Persistent WhatsApp conversation state machine.

    Redis contains only recoverable conversational state. The price becomes an
    immutable order snapshot at checkout and all eventual fulfillment data is
    written to PostgreSQL.
    """

    def __init__(self, store: RedisStore, cipher: PiiCipher, session_ttl: int) -> None:
        self.store = store
        self.cipher = cipher
        self.session_ttl = session_ttl
        self.payments = PaymentService()
        self.bookings = BookingService(store)

    async def handle(
        self, db: AsyncSession, merchant: Merchant, inbound: InboundChat
    ) -> list[OutboundMessage]:
        session = await self.store.get_session(merchant.id, inbound.wa_id) or self._fresh_session()
        action = inbound.action_id or self._normalise_text(inbound.text)
        text_actions = {"review cart": "review_cart", "empty cart": "empty_cart"}
        action = text_actions.get(action, action)

        if action in {"start", "menu", "catalog", "order", "hi", "hello", "restart"}:
            session = self._fresh_session()
            result = await self._send_catalog(db, merchant, inbound.wa_id, session)
            await self._save(merchant, inbound.wa_id, session)
            return result
        if action.startswith("staff:"):
            return await self._handle_staff_action(db, merchant, inbound, action)
        if action.startswith("item:"):
            result = await self._add_item(
                db, merchant, inbound.wa_id, session, action.removeprefix("item:")
            )
        elif action == "review_cart":
            result = await self._review_cart(db, merchant, inbound.wa_id, session)
        elif action == "empty_cart":
            session = self._fresh_session()
            result = await self._send_catalog(db, merchant, inbound.wa_id, session)
        elif action == "checkout":
            result = await self._checkout(db, merchant, inbound.wa_id, session)
        elif action.startswith("fulfillment:"):
            result = await self._select_fulfillment(
                db, merchant, inbound.wa_id, session, action.removeprefix("fulfillment:")
            )
        elif action == "custom_quote":
            result = await self._request_custom_quote(db, merchant, inbound.wa_id, session)
        elif action.startswith("booking_day:"):
            result = await self._select_booking_day(
                db, merchant, inbound.wa_id, session, action.removeprefix("booking_day:")
            )
        elif action.startswith("booking_slot:"):
            result = await self._select_booking_slot(
                db, merchant, inbound.wa_id, session, action.removeprefix("booking_slot:")
            )
        elif action.startswith("payment:"):
            result = await self._select_payment(
                db, merchant, inbound.wa_id, session, action.removeprefix("payment:")
            )
        elif inbound.location and session["state"] == "AWAITING_LOCATION":
            result = await self._save_location(
                db, merchant, inbound.wa_id, session, inbound.location
            )
        elif session["state"] == "AWAITING_LANDMARK" and inbound.text:
            result = await self._save_landmark(db, merchant, inbound.wa_id, session, inbound.text)
        elif session["state"] == "PAYMENT_PENDING_VERIFICATION" and inbound.text:
            result = await self._claim_manual_payment(
                db, merchant, inbound.wa_id, session, inbound.text
            )
        else:
            result = await self._send_catalog(db, merchant, inbound.wa_id, session)

        await self._save(merchant, inbound.wa_id, session)
        return result

    @staticmethod
    def _fresh_session() -> dict[str, Any]:
        return {"state": "BROWSING", "cart": {}, "order_id": None, "service_id": None}

    @staticmethod
    def _normalise_text(value: str | None) -> str:
        return value.strip().lower() if value else ""

    async def _save(self, merchant: Merchant, wa_id: str, session: dict[str, Any]) -> None:
        await self.store.save_session(merchant.id, wa_id, session, self.session_ttl)

    async def _send_catalog(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any]
    ) -> list[OutboundMessage]:
        items = list(
            (
                await db.scalars(
                    select(Item)
                    .where(Item.merchant_id == merchant.id, Item.active.is_(True))
                    .order_by(Item.category, Item.title)
                )
            ).all()
        )
        if not items:
            return [text(wa_id, "This catalogue is being updated. Please try again shortly.")]
        categories: dict[str, list[dict[str, str]]] = {}
        for item in items:
            price = local_price(
                item.base_price_usd, merchant.usd_exchange_rate, merchant.rounding_increment
            )
            categories.setdefault(item.category, []).append(
                {
                    "id": f"item:{item.id}",
                    "title": item.title[:24],
                    "description": f"{money(price, merchant.currency_code)} · {item.item_type.value.replace('_', ' ').title()}"[
                        :72
                    ],
                }
            )
        sections = [
            {"title": category[:24], "rows": rows[:10]}
            for category, rows in list(categories.items())[:10]
        ]
        session["state"] = "BROWSING"
        return [
            list_message(
                wa_id,
                f"Welcome to {merchant.business_name}. Choose an item:",
                "View items",
                sections,
            )
        ]

    async def _add_item(
        self,
        db: AsyncSession,
        merchant: Merchant,
        wa_id: str,
        session: dict[str, Any],
        item_id: str,
    ) -> list[OutboundMessage]:
        item = await db.scalar(
            select(Item).where(
                Item.id == item_id, Item.merchant_id == merchant.id, Item.active.is_(True)
            )
        )
        if not item:
            return [text(wa_id, "That item is no longer available. Please choose another item.")]
        if item.item_type == ItemType.SERVICE_BOOKING:
            session["cart"] = {item.id: 1}
            session["service_id"] = item.id
            session["state"] = "AWAITING_BOOKING_DAY"
            return self._booking_days(wa_id, item.title)
        cart = session.setdefault("cart", {})
        cart[item.id] = int(cart.get(item.id, 0)) + 1
        session["state"] = "BROWSING"
        return [
            buttons(
                wa_id,
                f"Added {item.title}. What would you like to do next?",
                [("catalog", "Add more"), ("review_cart", "Review cart"), ("checkout", "Checkout")],
            )
        ]

    async def _review_cart(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any]
    ) -> list[OutboundMessage]:
        cart = session.get("cart", {})
        if not cart:
            return await self._send_catalog(db, merchant, wa_id, session)
        items = list(
            (
                await db.scalars(
                    select(Item).where(
                        Item.id.in_(list(cart.keys())), Item.merchant_id == merchant.id
                    )
                )
            ).all()
        )
        indexed = {item.id: item for item in items}
        lines: list[str] = []
        subtotal = 0
        for item_id, quantity in cart.items():
            item = indexed.get(item_id)
            if not item or not item.active:
                continue
            unit = local_price(
                item.base_price_usd, merchant.usd_exchange_rate, merchant.rounding_increment
            )
            total = unit * int(quantity)
            subtotal += total
            lines.append(f"• {quantity}× {item.title} — {money(total, merchant.currency_code)}")
        if not lines:
            session["cart"] = {}
            return await self._send_catalog(db, merchant, wa_id, session)
        return [
            buttons(
                wa_id,
                "Your cart:\n"
                + "\n".join(lines)
                + f"\n\nSubtotal: {money(subtotal, merchant.currency_code)}",
                [("catalog", "Add more"), ("empty_cart", "Empty cart"), ("checkout", "Checkout")],
            )
        ]

    async def _create_order(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any]
    ) -> Order:
        if session.get("order_id"):
            existing = await db.get(Order, session["order_id"])
            if existing:
                return existing
        cart = session.get("cart", {})
        items = list(
            (
                await db.scalars(
                    select(Item).where(
                        Item.id.in_(list(cart.keys())), Item.merchant_id == merchant.id
                    )
                )
            ).all()
        )
        indexed = {item.id: item for item in items}
        customer = await db.scalar(
            select(Customer).where(
                Customer.merchant_id == merchant.id,
                Customer.wa_id_hash == self.cipher.fingerprint(wa_id),
            )
        )
        if not customer:
            customer = Customer(
                merchant_id=merchant.id,
                wa_id_hash=self.cipher.fingerprint(wa_id),
                wa_id_encrypted=self.cipher.encrypt(wa_id),
            )
            db.add(customer)
            await db.flush()
        order = Order(
            merchant_id=merchant.id,
            customer_id=customer.id,
            customer_wa_id_hash=self.cipher.fingerprint(wa_id),
            customer_wa_id_encrypted=self.cipher.encrypt(wa_id),
            reference=f"{uuid4().hex[:8].upper()}",
            currency_code=merchant.currency_code,
            exchange_rate_locked=merchant.usd_exchange_rate,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        for item_id, quantity in cart.items():
            item = indexed.get(item_id)
            if not item or not item.active:
                continue
            unit = local_price(
                item.base_price_usd, merchant.usd_exchange_rate, merchant.rounding_increment
            )
            line = OrderItem(
                merchant_id=merchant.id,
                item_id=item.id,
                title_snapshot=item.title,
                quantity=int(quantity),
                unit_price=unit,
                line_total=unit * int(quantity),
            )
            order.lines.append(line)
            order.subtotal_amount += line.line_total
        if not order.lines:
            raise ValueError("cart has no available items")
        order.total_amount = order.subtotal_amount
        db.add(order)
        await db.flush()
        session["order_id"] = order.id
        return order

    async def _checkout(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any]
    ) -> list[OutboundMessage]:
        if not session.get("cart"):
            return await self._send_catalog(db, merchant, wa_id, session)
        if session.get("service_id"):
            session["state"] = "AWAITING_BOOKING_DAY"
            return self._booking_days(wa_id, "your service")
        try:
            await self._create_order(db, merchant, wa_id, session)
        except ValueError:
            return await self._send_catalog(db, merchant, wa_id, session)
        session["state"] = "AWAITING_FULFILLMENT"
        return [
            buttons(
                wa_id,
                "How would you like to receive your order?",
                [
                    ("fulfillment:delivery", "Doorstep delivery"),
                    ("fulfillment:pickup", "Self pickup"),
                ],
            )
        ]

    async def _select_fulfillment(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any], choice: str
    ) -> list[OutboundMessage]:
        order = await self._create_order(db, merchant, wa_id, session)
        if choice == "delivery":
            order.fulfillment_type = FulfillmentType.DELIVERY
            order.delivery_fee = merchant.delivery_fee
            order.total_amount = order.subtotal_amount + merchant.delivery_fee
            order.status = OrderStatus.AWAITING_LOCATION
            session["state"] = "AWAITING_LOCATION"
            return [
                text(
                    wa_id,
                    "Please tap the attachment icon → Location → Send your current location pin.",
                )
            ]
        if choice == "pickup":
            order.fulfillment_type = FulfillmentType.PICKUP
            order.status = OrderStatus.AWAITING_PAYMENT_METHOD
            session["state"] = "AWAITING_PAYMENT_METHOD"
            return self._payment_options(wa_id, order)
        return [text(wa_id, "Please choose delivery or self pickup.")]

    async def _save_location(
        self,
        db: AsyncSession,
        merchant: Merchant,
        wa_id: str,
        session: dict[str, Any],
        location: dict[str, Any],
    ) -> list[OutboundMessage]:
        order = await self._order_from_session(db, session)
        if not order:
            return await self._send_catalog(db, merchant, wa_id, session)
        try:
            lat, lng = float(location["latitude"]), float(location["longitude"])
        except (KeyError, TypeError, ValueError):
            return [
                text(wa_id, "I need a WhatsApp location pin with coordinates. Please try again.")
            ]
        decision = validate_delivery_location(
            merchant.shop_latitude,
            merchant.shop_longitude,
            merchant.delivery_radius_km,
            lat,
            lng,
        )
        if not decision.eligible:
            if decision.distance_km is not None:
                return [
                    buttons(
                        wa_id,
                        f"Your pin is {decision.distance_km:.1f} km away, outside our delivery zone.",
                        [
                            ("fulfillment:pickup", "Switch to pickup"),
                            ("custom_quote", "Request courier quote"),
                        ],
                    )
                ]
            return [text(wa_id, decision.reason or "We cannot validate this delivery location.")]
        order.location_encrypted = self.cipher.encrypt(
            json.dumps({"latitude": lat, "longitude": lng, **location})
        )
        order.status = OrderStatus.AWAITING_LANDMARK
        session["state"] = "AWAITING_LANDMARK"
        return [
            text(
                wa_id,
                "Got your location. Reply with a nearby landmark or gate colour (for example, opposite TotalEnergies, black gate).",
            )
        ]

    async def _request_custom_quote(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any]
    ) -> list[OutboundMessage]:
        order = await self._order_from_session(db, session)
        if not order:
            return await self._send_catalog(db, merchant, wa_id, session)
        messages = [
            text(
                wa_id,
                "We have asked the merchant for a custom courier quote. They will reply shortly.",
            )
        ]
        if merchant.dispatch_channel_wa_id:
            messages.append(
                text(
                    merchant.dispatch_channel_wa_id,
                    f"CUSTOM COURIER QUOTE REQUEST: #{order.reference}\nCustomer: {wa_id}\n"
                    "The customer is outside the normal delivery zone. Reply directly with a courier quote.",
                )
            )
        return messages

    async def _save_landmark(
        self,
        db: AsyncSession,
        merchant: Merchant,
        wa_id: str,
        session: dict[str, Any],
        landmark: str,
    ) -> list[OutboundMessage]:
        order = await self._order_from_session(db, session)
        if not order:
            return await self._send_catalog(db, merchant, wa_id, session)
        order.landmark_encrypted = self.cipher.encrypt(landmark.strip()[:500])
        order.status = OrderStatus.AWAITING_PAYMENT_METHOD
        session["state"] = "AWAITING_PAYMENT_METHOD"
        return self._payment_options(wa_id, order)

    def _payment_options(self, wa_id: str, order: Order) -> list[OutboundMessage]:
        return [
            buttons(
                wa_id,
                f"Total due: {money(order.total_amount, order.currency_code)}. Choose payment:",
                [
                    ("payment:stk", "Mobile money"),
                    ("payment:manual", "Manual till"),
                    ("payment:cash", "Cash on delivery"),
                ],
            )
        ]

    def _booking_days(self, wa_id: str, service_name: str) -> list[OutboundMessage]:
        today = datetime.now(UTC).date()
        choices = [
            (f"booking_day:{(today + timedelta(days=offset)).isoformat()}", label)
            for offset, label in enumerate(["Today", "Tomorrow", "In 2 days"])
        ]
        return [buttons(wa_id, f"When would you like {service_name}?", choices)]

    async def _select_booking_day(
        self,
        db: AsyncSession,
        merchant: Merchant,
        wa_id: str,
        session: dict[str, Any],
        date_value: str,
    ) -> list[OutboundMessage]:
        if not session.get("service_id"):
            return await self._send_catalog(db, merchant, wa_id, session)
        try:
            day = datetime.fromisoformat(date_value).replace(tzinfo=UTC)
        except ValueError:
            return self._booking_days(wa_id, "your service")
        slots = await self.bookings.available_slots(
            db, merchant.id, session["service_id"], day, day + timedelta(days=1)
        )
        if not slots:
            return [
                text(wa_id, "No slots are open that day. Please choose another day."),
                *self._booking_days(wa_id, "your service"),
            ]
        rows = [
            {
                "id": f"booking_slot:{slot.id}",
                "title": slot.start_time.astimezone(UTC).strftime("%H:%M UTC"),
                "description": f"Ends {slot.end_time.astimezone(UTC).strftime('%H:%M UTC')}",
            }
            for slot in slots[:10]
        ]
        session["state"] = "AWAITING_BOOKING_SLOT"
        return [
            list_message(
                wa_id, "Available times:", "View times", [{"title": "Available", "rows": rows}]
            )
        ]

    async def _select_booking_slot(
        self,
        db: AsyncSession,
        merchant: Merchant,
        wa_id: str,
        session: dict[str, Any],
        slot_id: str,
    ) -> list[OutboundMessage]:
        try:
            slot = await self.bookings.hold_slot(
                db, merchant.id, session["service_id"], slot_id, wa_id
            )
        except SlotUnavailable:
            return [
                text(
                    wa_id,
                    "That slot was just selected by someone else. Please choose another time.",
                )
            ]
        order = await self._create_order(db, merchant, wa_id, session)
        order.booking_slot_id = slot.id
        order.fulfillment_type = FulfillmentType.BOOKING
        order.status = OrderStatus.AWAITING_PAYMENT_METHOD
        session["state"] = "AWAITING_PAYMENT_METHOD"
        return self._payment_options(wa_id, order)

    async def _select_payment(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any], choice: str
    ) -> list[OutboundMessage]:
        order = await self._order_from_session(db, session)
        if not order:
            return await self._send_catalog(db, merchant, wa_id, session)
        configured_stk = PaymentProvider[get_settings().default_stk_provider]
        providers = {
            "stk": configured_stk,
            "manual": PaymentProvider.MANUAL_TILL,
            "cash": PaymentProvider.CASH_ON_DELIVERY,
        }
        provider = providers.get(choice)
        if not provider:
            return self._payment_options(wa_id, order)
        payment = await self.payments.initiate(db, order, provider)
        if provider in {
            PaymentProvider.MTN_MOMO,
            PaymentProvider.AIRTEL_MONEY,
            PaymentProvider.MPESA,
        }:
            # Persist the provider-visible external reference before any carrier request can
            # result in a fast callback.
            await db.commit()
            await set_tenant(db, merchant.id)
            stk = await self.payments.request_stk(payment, order, merchant.business_name, wa_id)
            if not stk.accepted:
                order.status = OrderStatus.AWAITING_PAYMENT_RECOVERY
                session["state"] = "AWAITING_PAYMENT_METHOD"
                return [
                    buttons(
                        wa_id,
                        f"We could not start the mobile-money prompt ({stk.reason}). Choose a fallback:",
                        [
                            ("payment:stk", "Retry prompt"),
                            ("payment:manual", "Manual till"),
                            ("payment:cash", "Cash on delivery"),
                        ],
                    )
                ]
            ledger = await db.get(PaymentLedger, payment.payment_id)
            if ledger:
                ledger.provider_reference = stk.provider_reference
            session["state"] = "AWAITING_PAYMENT_CALLBACK"
            return [
                text(
                    wa_id,
                    "A Mobile Money prompt has been sent to your phone. Enter your PIN to pay. "
                    "If it does not appear within 90 seconds, choose Manual Till.",
                )
            ]
        if provider == PaymentProvider.MANUAL_TILL:
            session["state"] = "PAYMENT_PENDING_VERIFICATION"
            details = merchant.manual_payment_details or {}
            provider_name = details.get("provider", "Mobile Money")
            till = details.get("till", "contact merchant")
            account_name = details.get("account_name", merchant.business_name)
            return [
                text(
                    wa_id,
                    f"Please transfer {money(order.total_amount, order.currency_code)} to:\n"
                    f"• Provider: {provider_name}\n• Merchant code / Till: {till}\n• Name: {account_name}\n\n"
                    "Then reply with the carrier transaction ID or send the confirmation screenshot.",
                )
            ]
        session["state"] = "PAID"
        return await self.dispatch_paid_order(
            db, merchant, order, customer_note="Your cash-on-delivery order is confirmed."
        )

    async def _claim_manual_payment(
        self, db: AsyncSession, merchant: Merchant, wa_id: str, session: dict[str, Any], source: str
    ) -> list[OutboundMessage]:
        order = await self._order_from_session(db, session)
        if not order:
            return await self._send_catalog(db, merchant, wa_id, session)
        ledger = await self.payments.claim_manual_transaction(db, order, source)
        if not ledger:
            return [
                text(
                    wa_id,
                    "I could not find a transaction ID. Please reply with the ID from your payment confirmation.",
                )
            ]
        session["state"] = "PAYMENT_PENDING_VERIFICATION"
        customer_message = text(wa_id, "Thanks. Your payment is awaiting merchant verification.")
        if not merchant.dispatch_channel_wa_id:
            return [customer_message]
        staff_message = buttons(
            merchant.dispatch_channel_wa_id,
            f"⚠️ PAYMENT CLAIMED: #{order.reference}\nCustomer: {self._customer_wa_id(order)}\n"
            f"Claimed TXID: {ledger.claimed_transaction_id}\nAmount: {money(order.total_amount, order.currency_code)}",
            [
                (f"staff:payment:{order.id}:approve", "Approve payment"),
                (f"staff:payment:{order.id}:reject", "Reject payment"),
            ],
        )
        return [customer_message, staff_message]

    async def dispatch_paid_order(
        self, db: AsyncSession, merchant: Merchant, order: Order, customer_note: str | None = None
    ) -> list[OutboundMessage]:
        """Make payment success visible to customer and dispatch team exactly once per caller."""

        if order.booking_slot_id:
            try:
                await self.bookings.confirm_slot(db, order, self._customer_wa_id(order))
            except SlotUnavailable:
                order.status = OrderStatus.AWAITING_PAYMENT_RECOVERY
                return [
                    text(
                        self._customer_wa_id(order),
                        "We received your payment but the appointment slot is no longer available. "
                        "The merchant will contact you immediately to rebook or refund you.",
                    )
                ]
        messages = [
            text(
                self._customer_wa_id(order),
                customer_note or "Payment confirmed. Your order is now with the merchant.",
            )
        ]
        if merchant.dispatch_channel_wa_id:
            items = list(
                (await db.scalars(select(OrderItem).where(OrderItem.order_id == order.id))).all()
            )
            item_lines = "\n".join(f"• {line.quantity}× {line.title_snapshot}" for line in items)
            messages.append(
                buttons(
                    merchant.dispatch_channel_wa_id,
                    f"🚨 NEW ORDER RECEIVED: #{order.reference}\nCustomer: {self._customer_wa_id(order)}\n"
                    f"Type: {(order.fulfillment_type.value if order.fulfillment_type else 'ORDER')}\n"
                    f"Items:\n{item_lines}\nTotal: {money(order.total_amount, order.currency_code)} [PAID]",
                    [
                        (f"staff:order:{order.id}:prepare", "Accept & prep"),
                        (f"staff:order:{order.id}:dispatch", "Rider dispatched"),
                        (f"staff:order:{order.id}:cancel", "Cancel order"),
                    ],
                )
            )
        return messages

    async def _handle_staff_action(
        self, db: AsyncSession, merchant: Merchant, inbound: InboundChat, action: str
    ) -> list[OutboundMessage]:
        parts = action.split(":")
        if len(parts) != 4:
            return [text(inbound.wa_id, "That staff action is invalid.")]
        _, kind, order_id, decision = parts
        order = await db.get(Order, order_id, with_for_update=True)
        if not order or order.merchant_id != merchant.id:
            return [text(inbound.wa_id, "Order not found for this merchant.")]
        if kind == "payment" and decision in {"approve", "reject"}:
            updated = await self.payments.staff_decision(
                db, order_id, decision == "approve", self.cipher.encrypt(inbound.wa_id) or ""
            )
            if not updated:
                return [text(inbound.wa_id, "Payment record not found.")]
            if decision == "approve":
                return [
                    text(inbound.wa_id, "Payment approved."),
                    *await self.dispatch_paid_order(db, merchant, updated),
                ]
            return [
                text(inbound.wa_id, "Payment rejected."),
                text(
                    self._customer_wa_id(updated),
                    "We could not verify that payment. Please retry or choose another payment method.",
                ),
            ]
        if kind == "order":
            updates = {
                "prepare": (OrderStatus.PREPARING, "Your order is being prepared."),
                "dispatch": (OrderStatus.DISPATCHED, "Your order has been dispatched."),
                "cancel": (
                    OrderStatus.CANCELLED,
                    "Your order has been cancelled. The merchant will contact you about any refund.",
                ),
            }
            if decision not in updates:
                return [text(inbound.wa_id, "Unknown order action.")]
            order.status, customer_text = updates[decision]
            db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_wa_id_encrypted=self.cipher.encrypt(inbound.wa_id),
                    event_type=f"order_{decision}",
                    entity_type="order",
                    entity_id=order.id,
                    detail={},
                )
            )
            return [
                text(inbound.wa_id, f"Order #{order.reference} updated."),
                text(self._customer_wa_id(order), customer_text),
            ]
        return [text(inbound.wa_id, "That staff action is invalid.")]

    @staticmethod
    async def _order_from_session(db: AsyncSession, session: dict[str, Any]) -> Order | None:
        order_id = session.get("order_id")
        return await db.get(Order, order_id) if order_id else None

    def _customer_wa_id(self, order: Order) -> str:
        value = self.cipher.decrypt(order.customer_wa_id_encrypted)
        if not value:
            raise ValueError("order is missing an encrypted WhatsApp ID")
        return value
