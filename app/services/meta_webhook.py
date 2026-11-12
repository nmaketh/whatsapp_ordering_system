from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedInboundEvent:
    message_id: str
    phone_number_id: str
    wa_id: str
    display_name: str | None
    text: str | None
    action_id: str | None
    location: dict[str, Any] | None
    raw: dict[str, Any]


def parse_meta_messages(payload: dict[str, Any]) -> list[ParsedInboundEvent]:
    """Extract customer messages from a WhatsApp Cloud API webhook safely.

    Delivery/read statuses are intentionally not passed into the conversation
    engine; they are transport events, not customer intent.
    """

    events: list[ParsedInboundEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")
            contacts = {
                contact.get("wa_id"): contact.get("profile", {}).get("name")
                for contact in value.get("contacts", [])
            }
            if not phone_number_id:
                continue
            for message in value.get("messages", []):
                message_id = message.get("id")
                wa_id = message.get("from")
                if not message_id or not wa_id:
                    continue
                message_type = message.get("type")
                text = message.get("text", {}).get("body") if message_type == "text" else None
                interactive = (
                    message.get("interactive", {}) if message_type == "interactive" else {}
                )
                button_reply = interactive.get("button_reply", {})
                list_reply = interactive.get("list_reply", {})
                action_id = button_reply.get("id") or list_reply.get("id")
                location = message.get("location") if message_type == "location" else None
                events.append(
                    ParsedInboundEvent(
                        message_id=message_id,
                        phone_number_id=phone_number_id,
                        wa_id=wa_id,
                        display_name=contacts.get(wa_id),
                        text=text,
                        action_id=action_id,
                        location=location,
                        raw=message,
                    )
                )
    return events


def normalise_payment_callback(payload: dict[str, Any]) -> tuple[str, bool, str | None]:
    """Provider-neutral callback mapping for adapters to call.

    MNO provider adapters should translate their native callback body into these
    fields before invoking the settlement service. This accepts a common test
    envelope and avoids assuming one provider's proprietary event shape.
    """

    reference = payload.get("merchant_reference") or payload.get("external_id")
    status = str(payload.get("status", "")).upper()
    if not reference:
        raise ValueError("payment callback is missing merchant_reference")
    successful = status in {"SUCCESS", "SUCCESSFUL", "COMPLETED", "PAID"}
    provider_reference = payload.get("provider_reference") or payload.get("transaction_id")
    return str(reference), successful, str(provider_reference) if provider_reference else None


def merchant_id_from_payment_reference(reference: str) -> str:
    parts = reference.split(".")
    if len(parts) < 4 or parts[0] != "KZF" or not parts[1]:
        raise ValueError("payment callback has an invalid merchant reference")
    return parts[1]
