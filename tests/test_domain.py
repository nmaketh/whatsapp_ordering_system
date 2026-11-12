import hashlib
import hmac

from app.core.security import PiiCipher, valid_meta_signature
from app.services.currency import local_price, money
from app.services.geo import haversine_km, validate_delivery_location
from app.services.meta_webhook import merchant_id_from_payment_reference, parse_meta_messages
from app.services.payments import extract_transaction_id


def test_currency_rounds_up_to_clean_denomination() -> None:
    assert local_price(9.01, 3700, 500) == 33500
    assert money(33500, "UGX") == "UGX 33,500"


def test_delivery_radius_uses_coordinates() -> None:
    distance = haversine_km(0.3136, 32.5811, 0.314, 32.582)
    assert 0 < distance < 1
    assert validate_delivery_location(0.3136, 32.5811, 1, 0.314, 32.582).eligible
    decision = validate_delivery_location(0.3136, 32.5811, 1, 0.5, 32.8)
    assert not decision.eligible
    assert decision.reason == "outside_delivery_zone"


def test_pii_cipher_round_trip_and_signature_validation() -> None:
    cipher = PiiCipher(None)
    encrypted = cipher.encrypt("behind the blue gate")
    assert encrypted != "behind the blue gate"
    assert cipher.decrypt(encrypted) == "behind the blue gate"
    body = b'{"hello":"world"}'
    secret = "app-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert valid_meta_signature(body, signature, secret)
    assert not valid_meta_signature(body, signature, "wrong-secret")


def test_meta_parser_and_payment_reference() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "contacts": [{"wa_id": "256700000000", "profile": {"name": "Nhial"}}],
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "256700000000",
                                    "type": "interactive",
                                    "interactive": {"list_reply": {"id": "item:abc"}},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    event = parse_meta_messages(payload)[0]
    assert event.phone_number_id == "phone-1"
    assert event.display_name == "Nhial"
    assert event.action_id == "item:abc"
    assert merchant_id_from_payment_reference("KZF.merchant-42.ORD123.ABCDEF12") == "merchant-42"


def test_manual_transaction_id_extraction_avoids_plain_words() -> None:
    assert extract_transaction_id("I paid ABC-2093847291") == "ABC-2093847291"
    assert extract_transaction_id("confirmed today thanks") is None
