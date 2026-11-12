from decimal import ROUND_CEILING, Decimal


def local_price(
    base_price_usd: Decimal | float, exchange_rate: Decimal | float, increment: int
) -> int:
    """Convert and always round up to a merchant's clean note denomination."""

    if increment < 1:
        raise ValueError("rounding increment must be positive")
    raw = Decimal(str(base_price_usd)) * Decimal(str(exchange_rate))
    return int((raw / Decimal(increment)).to_integral_value(rounding=ROUND_CEILING) * increment)


def money(amount: int, currency: str) -> str:
    return f"{currency} {amount:,.0f}"
