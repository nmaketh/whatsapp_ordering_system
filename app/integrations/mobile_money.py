from dataclasses import dataclass
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.db.models import PaymentProvider


@dataclass(frozen=True)
class StkRequest:
    provider: PaymentProvider
    merchant_reference: str
    amount: int
    currency_code: str
    customer_wa_id: str
    merchant_name: str


@dataclass(frozen=True)
class StkResult:
    accepted: bool
    provider_reference: str | None = None
    reason: str | None = None


class MobileMoneyGateway:
    """Provider boundary for MTN MoMo, Airtel Money and Safaricom Daraja.

    It uses pre-issued bearer credentials because token acquisition differs by
    merchant/provider agreement. A deployment can rotate them from a secrets
    manager or subclass this adapter for a provider-specific OAuth flow.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request_to_pay(self, request: StkRequest) -> StkResult:
        if request.provider == PaymentProvider.MTN_MOMO:
            return await self._mtn(request)
        if request.provider == PaymentProvider.AIRTEL_MONEY:
            return await self._airtel(request)
        if request.provider == PaymentProvider.MPESA:
            return await self._mpesa(request)
        return StkResult(False, reason="provider does not support STK push")

    async def _mtn(self, request: StkRequest) -> StkResult:
        url = self.settings.mtn_momo_request_to_pay_url
        token = self.settings.mtn_momo_bearer_token
        subscription_key = self.settings.mtn_momo_subscription_key
        if not (url and token and subscription_key):
            return StkResult(False, reason="MTN MoMo is not configured")
        reference = str(uuid4())
        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Ocp-Apim-Subscription-Key": subscription_key.get_secret_value(),
            "X-Reference-Id": reference,
            "X-Target-Environment": self.settings.mtn_momo_target_environment,
            "Content-Type": "application/json",
        }
        payload = {
            "amount": str(request.amount),
            "currency": request.currency_code,
            "externalId": request.merchant_reference,
            "payer": {"partyIdType": "MSISDN", "partyId": request.customer_wa_id},
            "payerMessage": f"Pay {request.merchant_name}",
            "payeeNote": request.merchant_reference,
        }
        return await self._post(url, headers, payload, reference)

    async def _airtel(self, request: StkRequest) -> StkResult:
        url = self.settings.airtel_money_payment_url
        token = self.settings.airtel_money_bearer_token
        if not (url and token):
            return StkResult(False, reason="Airtel Money is not configured")
        payload = {
            "reference": request.merchant_reference,
            "subscriber": {
                "country": request.currency_code[:2],
                "currency": request.currency_code,
                "msisdn": request.customer_wa_id,
            },
            "transaction": {
                "amount": request.amount,
                "country": request.currency_code[:2],
                "currency": request.currency_code,
                "id": request.merchant_reference,
            },
        }
        return await self._post(
            url,
            {"Authorization": f"Bearer {token.get_secret_value()}"},
            payload,
            request.merchant_reference,
        )

    async def _mpesa(self, request: StkRequest) -> StkResult:
        url = self.settings.mpesa_stk_push_url
        token = self.settings.mpesa_bearer_token
        if not (url and token):
            return StkResult(False, reason="M-Pesa Daraja is not configured")
        # Daraja deployments should inject their own timestamp/password/callback URL through a
        # merchant-specific adapter. This payload has the canonical business fields.
        payload = {
            "AccountReference": request.merchant_reference,
            "Amount": request.amount,
            "PartyA": request.customer_wa_id,
            "PhoneNumber": request.customer_wa_id,
            "TransactionDesc": f"Pay {request.merchant_name}",
        }
        return await self._post(
            url,
            {"Authorization": f"Bearer {token.get_secret_value()}"},
            payload,
            request.merchant_reference,
        )

    async def _post(
        self, url: str, headers: dict[str, str], payload: dict, reference: str
    ) -> StkResult:
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
            if 200 <= response.status_code < 300:
                body = response.json() if response.content else {}
                return StkResult(
                    True, str(body.get("referenceId") or body.get("CheckoutRequestID") or reference)
                )
            return StkResult(False, reason=f"provider returned HTTP {response.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            return StkResult(False, reason=f"provider connection failed: {type(exc).__name__}")
