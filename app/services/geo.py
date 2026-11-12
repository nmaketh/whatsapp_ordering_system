import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryDecision:
    eligible: bool
    distance_km: float | None
    reason: str | None = None


def haversine_km(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    radius_km = 6371.0088
    lat_delta = math.radians(lat_b - lat_a)
    lng_delta = math.radians(lng_b - lng_a)
    a = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(lat_a))
        * math.cos(math.radians(lat_b))
        * math.sin(lng_delta / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def validate_delivery_location(
    shop_lat: float | None,
    shop_lng: float | None,
    radius_km: float,
    customer_lat: float | None,
    customer_lng: float | None,
) -> DeliveryDecision:
    if customer_lat is None or customer_lng is None:
        return DeliveryDecision(
            False, None, "Please share a WhatsApp location pin, not a text address."
        )
    if not -90 <= customer_lat <= 90 or not -180 <= customer_lng <= 180:
        return DeliveryDecision(False, None, "The location coordinates are invalid.")
    if shop_lat is None or shop_lng is None:
        # A merchant that has not configured a shop pin deliberately falls back to manual quote.
        return DeliveryDecision(
            False, None, "Delivery area is not yet configured. Request a courier quote."
        )
    distance = haversine_km(float(shop_lat), float(shop_lng), customer_lat, customer_lng)
    if distance > float(radius_km):
        return DeliveryDecision(False, distance, "outside_delivery_zone")
    return DeliveryDecision(True, distance)
