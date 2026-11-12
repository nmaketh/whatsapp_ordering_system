from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.services.redis_store import RedisStore


@dataclass(frozen=True)
class OutboundMessage:
    recipient: str
    payload: dict[str, Any]


def text(recipient: str, body: str) -> OutboundMessage:
    return OutboundMessage(recipient, {"type": "text", "text": {"body": body}})


def buttons(recipient: str, body: str, choices: list[tuple[str, str]]) -> OutboundMessage:
    if not 1 <= len(choices) <= 3:
        raise ValueError("WhatsApp reply buttons require 1-3 choices")
    return OutboundMessage(
        recipient,
        {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": identifier, "title": title[:20]}}
                        for identifier, title in choices
                    ]
                },
            },
        },
    )


def list_message(
    recipient: str, body: str, button_label: str, sections: list[dict[str, Any]]
) -> OutboundMessage:
    return OutboundMessage(
        recipient,
        {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {"button": button_label[:20], "sections": sections[:10]},
            },
        },
    )


class WhatsAppClient:
    def __init__(self, settings: Settings, store: RedisStore) -> None:
        self.settings = settings
        self.store = store

    async def send(self, phone_number_id: str, message: OutboundMessage) -> dict[str, Any]:
        if not await self.store.consume_rate_token(
            f"outbound:{phone_number_id}",
            self.settings.outbound_messages_per_second,
            self.settings.outbound_messages_per_second,
        ):
            raise RuntimeError("WhatsApp outbound rate limit reached")
        if not self.settings.meta_access_token:
            # Local development deliberately logs a structured result instead of talking to Meta.
            return {"development": True, "to": message.recipient, **message.payload}
        payload = {"messaging_product": "whatsapp", "to": message.recipient, **message.payload}
        url = f"https://graph.facebook.com/{self.settings.meta_graph_version}/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.settings.meta_access_token.get_secret_value()}"}
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
