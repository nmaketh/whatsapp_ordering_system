import json
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import Settings
from app.core.security import PiiCipher

INBOUND_STREAM = "kaziflow:inbound"
DLQ_STREAM = "kaziflow:dead-letter"
OUTBOUND_STREAM = "kaziflow:outbound"


class RedisStore:
    def __init__(self, client: Redis, cipher: PiiCipher) -> None:
        self.client = client
        self.cipher = cipher

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisStore":
        cipher = PiiCipher(
            settings.pii_encryption_key.get_secret_value() if settings.pii_encryption_key else None
        )
        return cls(Redis.from_url(settings.redis_url, decode_responses=True), cipher)

    def _pack(self, value: dict[str, Any]) -> str:
        encrypted = self.cipher.encrypt(json.dumps(value, separators=(",", ":")))
        if not encrypted:
            raise ValueError("cannot encrypt Redis payload")
        return encrypted

    def unpack(self, value: str) -> dict[str, Any]:
        decrypted = self.cipher.decrypt(value)
        if not decrypted:
            raise ValueError("cannot decrypt Redis payload")
        return json.loads(decrypted)

    def _customer_token(self, customer_wa_id: str) -> str:
        return self.cipher.fingerprint(customer_wa_id)

    async def close(self) -> None:
        await self.client.aclose()

    async def claim_message(self, message_id: str) -> bool:
        return bool(await self.client.set(f"message:{message_id}", "1", ex=86400, nx=True))

    async def publish_inbound(self, payload: dict[str, Any]) -> str:
        return await self.client.xadd(INBOUND_STREAM, {"payload": self._pack(payload)})

    async def publish_dead_letter(self, payload: dict[str, Any], reason: str) -> str:
        return await self.client.xadd(
            DLQ_STREAM,
            {"payload": self._pack(payload), "reason": reason},
        )

    async def publish_outbound(
        self, phone_number_id: str, recipient: str, payload: dict[str, Any]
    ) -> str:
        return await self.client.xadd(
            OUTBOUND_STREAM,
            {
                "payload": self._pack(
                    {"phone_number_id": phone_number_id, "recipient": recipient, "message": payload}
                )
            },
        )

    async def get_session(self, merchant_id: str, customer_wa_id: str) -> dict[str, Any] | None:
        data = await self.client.get(
            f"session:{merchant_id}:{self._customer_token(customer_wa_id)}"
        )
        return self.unpack(data) if data else None

    async def save_session(
        self, merchant_id: str, customer_wa_id: str, session: dict[str, Any], ttl: int
    ) -> None:
        session["updated_at_epoch"] = int(time.time())
        session["customer_wa_id_encrypted"] = self.cipher.encrypt(customer_wa_id)
        await self.client.set(
            f"session:{merchant_id}:{self._customer_token(customer_wa_id)}",
            self._pack(session),
            ex=ttl,
        )

    async def iter_sessions(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Return active sessions as (merchant_id, wa_id, state); intended for low-rate housekeeping."""

        cursor = 0
        sessions: list[tuple[str, str, dict[str, Any]]] = []
        while True:
            cursor, keys = await self.client.scan(cursor=cursor, match="session:*", count=100)
            for key in keys:
                raw = await self.client.get(key)
                parts = key.split(":", 2)
                if raw and len(parts) == 3:
                    session = self.unpack(raw)
                    wa_id = self.cipher.decrypt(session.get("customer_wa_id_encrypted"))
                    if wa_id:
                        sessions.append((parts[1], wa_id, session))
            if cursor == 0:
                return sessions

    async def clear_session(self, merchant_id: str, customer_wa_id: str) -> None:
        await self.client.delete(f"session:{merchant_id}:{self._customer_token(customer_wa_id)}")

    async def acquire_slot_lock(
        self, merchant_id: str, service_id: str, timestamp: str, customer_wa_id: str, ttl: int = 600
    ) -> bool:
        key = f"lock:slot:{merchant_id}:{service_id}:{timestamp}"
        return bool(
            await self.client.set(key, self._customer_token(customer_wa_id), ex=ttl, nx=True)
        )

    async def release_slot_lock(
        self, merchant_id: str, service_id: str, timestamp: str, customer_wa_id: str
    ) -> bool:
        key = f"lock:slot:{merchant_id}:{service_id}:{timestamp}"
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        return bool(await self.client.eval(script, 1, key, self._customer_token(customer_wa_id)))

    async def is_slot_locked(self, merchant_id: str, service_id: str, timestamp: str) -> bool:
        return bool(await self.client.exists(f"lock:slot:{merchant_id}:{service_id}:{timestamp}"))

    async def acquire_customer_lock(
        self, merchant_id: str, customer_wa_id: str, ttl: int = 30
    ) -> bool:
        return bool(
            await self.client.set(
                f"processing:{merchant_id}:{self._customer_token(customer_wa_id)}",
                str(time.time()),
                ex=ttl,
                nx=True,
            )
        )

    async def release_customer_lock(self, merchant_id: str, customer_wa_id: str) -> None:
        await self.client.delete(f"processing:{merchant_id}:{self._customer_token(customer_wa_id)}")

    async def consume_rate_token(self, bucket: str, capacity: int, per_second: int) -> bool:
        """Atomic token bucket; returns false when the outbound limit is exhausted."""

        key = f"rate:{bucket}"
        now = int(time.time() * 1000)
        script = """
        local current = redis.call('HMGET', KEYS[1], 'tokens', 'last')
        local tokens = tonumber(current[1]) or tonumber(ARGV[1])
        local last = tonumber(current[2]) or tonumber(ARGV[3])
        local elapsed = math.max(0, tonumber(ARGV[3]) - last)
        tokens = math.min(tonumber(ARGV[1]), tokens + (elapsed * tonumber(ARGV[2]) / 1000))
        if tokens < 1 then
          redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last', ARGV[3])
          redis.call('PEXPIRE', KEYS[1], 2000)
          return 0
        end
        redis.call('HMSET', KEYS[1], 'tokens', tokens - 1, 'last', ARGV[3])
        redis.call('PEXPIRE', KEYS[1], 2000)
        return 1
        """
        return bool(await self.client.eval(script, 1, key, capacity, per_second, now))

    async def ensure_consumer_group(self, group: str) -> None:
        try:
            await self.client.xgroup_create(INBOUND_STREAM, group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def ensure_outbound_consumer_group(self, group: str) -> None:
        try:
            await self.client.xgroup_create(OUTBOUND_STREAM, group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
