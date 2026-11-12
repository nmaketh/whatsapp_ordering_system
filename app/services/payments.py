import re
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    AuditEvent,
    Order,
    OrderStatus,
    PaymentLedger,
    PaymentProvider,
    PaymentStatus,
)
from app.integrations.mobile_money import MobileMoneyGateway, StkRequest, StkResult

TXID_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9-]{5,39})(?![A-Z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class PaymentInitiation:
    merchant_reference: str
    payment_id: str
    provider: PaymentProvider


class PaymentService:
    """Ledger-first payment orchestration.

    Carrier-specific SDK calls plug in after ``initiate`` has committed the
    auditable payment intent. No carrier callback may create an order itself.
    """

    async def initiate(
        self,
        db: AsyncSession,
        order: Order,
        provider: PaymentProvider,
    ) -> PaymentInitiation:
        # The provider echoes this opaque field in its callback. Embedding the
        # tenant ID lets the callback receiver establish PostgreSQL RLS context
        # before it looks up the ledger.
        reference = f"KZF.{order.merchant_id}.{order.reference}.{uuid4().hex[:8].upper()}"
        ledger = PaymentLedger(
            merchant_id=order.merchant_id,
            order_id=order.id,
            provider=provider,
            merchant_reference=reference,
            amount=order.total_amount,
            currency_code=order.currency_code,
            status=PaymentStatus.PENDING
            if provider not in {PaymentProvider.MANUAL_TILL, PaymentProvider.CASH_ON_DELIVERY}
            else PaymentStatus.INITIATED,
        )
        if provider in {
            PaymentProvider.MTN_MOMO,
            PaymentProvider.AIRTEL_MONEY,
            PaymentProvider.MPESA,
        }:
            order.status = OrderStatus.AWAITING_PAYMENT_CALLBACK
        elif provider == PaymentProvider.MANUAL_TILL:
            order.status = OrderStatus.PAYMENT_PENDING_VERIFICATION
        elif provider == PaymentProvider.CASH_ON_DELIVERY:
            order.status = OrderStatus.PAID
            ledger.status = PaymentStatus.SUCCESSFUL
        db.add(ledger)
        db.add(
            AuditEvent(
                merchant_id=order.merchant_id,
                actor_wa_id_encrypted=None,
                event_type="payment_initiated",
                entity_type="order",
                entity_id=order.id,
                detail={"provider": provider.value, "reference": reference},
            )
        )
        await db.flush()
        return PaymentInitiation(reference, ledger.id, provider)

    async def request_stk(
        self, initiation: PaymentInitiation, order: Order, merchant_name: str, customer_wa_id: str
    ) -> StkResult:
        return await MobileMoneyGateway(get_settings()).request_to_pay(
            StkRequest(
                provider=initiation.provider,
                merchant_reference=initiation.merchant_reference,
                amount=order.total_amount,
                currency_code=order.currency_code,
                customer_wa_id=customer_wa_id,
                merchant_name=merchant_name,
            )
        )

    async def claim_manual_transaction(
        self, db: AsyncSession, order: Order, source_text: str
    ) -> PaymentLedger | None:
        txid = extract_transaction_id(source_text)
        if not txid:
            return None
        ledger = await db.scalar(
            select(PaymentLedger)
            .where(PaymentLedger.order_id == order.id)
            .order_by(PaymentLedger.created_at.desc())
            .limit(1)
        )
        if not ledger:
            return None
        ledger.claimed_transaction_id = txid
        ledger.status = PaymentStatus.CLAIMED
        order.status = OrderStatus.PAYMENT_PENDING_VERIFICATION
        db.add(
            AuditEvent(
                merchant_id=order.merchant_id,
                actor_wa_id_encrypted=None,
                event_type="manual_payment_claimed",
                entity_type="payment",
                entity_id=ledger.id,
                detail={"transaction_id": txid},
            )
        )
        return ledger

    async def settle_callback(
        self,
        db: AsyncSession,
        merchant_reference: str,
        successful: bool,
        provider_reference: str | None,
        raw_callback: dict,
    ) -> Order | None:
        ledger = await db.scalar(
            select(PaymentLedger)
            .where(PaymentLedger.merchant_reference == merchant_reference)
            .with_for_update()
        )
        if not ledger:
            return None
        # Idempotency: a second success callback cannot cause a second fulfillment event.
        if ledger.status == PaymentStatus.SUCCESSFUL:
            return await db.get(Order, ledger.order_id)
        ledger.provider_reference = provider_reference
        ledger.raw_callback = raw_callback
        ledger.status = PaymentStatus.SUCCESSFUL if successful else PaymentStatus.FAILED
        order = await db.get(Order, ledger.order_id, with_for_update=True)
        if order:
            order.status = OrderStatus.PAID if successful else OrderStatus.AWAITING_PAYMENT_RECOVERY
            db.add(
                AuditEvent(
                    merchant_id=order.merchant_id,
                    actor_wa_id_encrypted=None,
                    event_type="payment_settled" if successful else "payment_failed",
                    entity_type="payment",
                    entity_id=ledger.id,
                    detail={"provider_reference": provider_reference},
                )
            )
        return order

    async def staff_decision(
        self, db: AsyncSession, order_id: str, approved: bool, actor_wa_id_encrypted: str
    ) -> Order | None:
        order = await db.get(Order, order_id, with_for_update=True)
        if not order:
            return None
        ledger = await db.scalar(
            select(PaymentLedger)
            .where(PaymentLedger.order_id == order.id)
            .order_by(PaymentLedger.created_at.desc())
            .limit(1)
        )
        if not ledger:
            return None
        ledger.status = PaymentStatus.SUCCESSFUL if approved else PaymentStatus.REJECTED
        order.status = OrderStatus.PAID if approved else OrderStatus.AWAITING_PAYMENT_RECOVERY
        db.add(
            AuditEvent(
                merchant_id=order.merchant_id,
                actor_wa_id_encrypted=actor_wa_id_encrypted,
                event_type="manual_payment_approved" if approved else "manual_payment_rejected",
                entity_type="order",
                entity_id=order.id,
                detail={},
            )
        )
        return order


def extract_transaction_id(text: str) -> str | None:
    """Find likely MNO references while rejecting ordinary short words/numbers."""

    candidates = TXID_PATTERN.findall(text.upper())
    for candidate in candidates:
        if any(char.isdigit() for char in candidate):
            return candidate
    return None
