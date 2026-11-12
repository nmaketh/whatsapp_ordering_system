# KaziFlow Engine

KaziFlow is a WhatsApp-native ordering and appointment platform designed for East African operating conditions: MNO-wallet payments, unstable carrier callbacks, landmark-based delivery, dynamic local-currency pricing, and merchant fulfilment directly from WhatsApp.

## What is included

- FastAPI Meta webhook gateway with verification handshake, HMAC-SHA256 validation, Redis-backed 24-hour message deduplication, and immediate stream ingestion.
- Redis Streams workers for inbound conversation processing, dead letters, rate-limited outbound Meta dispatch, cart sessions, and Redis `SET NX EX` booking holds.
- PostgreSQL multi-tenant domain for merchants, catalog items, customers, orders, immutable order lines, booking slots, payment ledger, and audit events.
- PostgreSQL RLS policies on every merchant-owned table. The application sets `app.current_merchant` transaction-locally before a tenant query.
- WhatsApp Interactive List/Reply Button flows for catalog ordering, cart review, delivery pins, landmarks, payment fallbacks, booking dates/times, and merchant group workflow buttons.
- Dynamic USD-to-local price calculation with per-merchant upward rounding; exchange rates are snapshotted at checkout.
- AES-256-GCM encrypted delivery and landmark fields, plus a seven-day completed/cancelled-order PII scrub job.
- Mobile-money adapter boundary for MTN MoMo, Airtel Money, and M-Pesa/Daraja; manual till transaction-ID verification and cash-on-delivery work without provider credentials.

## Run locally

1. Copy `.env.example` to `.env` and set non-development secrets.

2. Start the stack:

   ```powershell
   docker compose up --build
   ```

   The API runs at `http://localhost:8000`; OpenAPI is at `/docs`.

3. Create a merchant and items via the protected admin API. In development, `ADMIN_API_KEY` may be unset; production refuses an unset admin key.

   ```powershell
   $headers = @{ "X-KaziFlow-Admin-Key" = "change-this-before-exposing-admin-api" }
   Invoke-RestMethod http://localhost:8000/v1/merchants -Method Post -Headers $headers -ContentType 'application/json' -Body '{
     "business_name":"Kazi Kitchen", "waba_phone_number_id":"123456789",
     "currency_code":"UGX", "usd_exchange_rate":3700, "rounding_increment":500,
     "manual_payment_details":{"provider":"MTN MoMo","till":"654321","account_name":"KAZI KITCHEN"},
     "shop_latitude":0.3136,"shop_longitude":32.5811,"delivery_radius_km":7,"delivery_fee":3000
   }'
   ```

4. In Meta, configure `GET/POST https://<host>/webhooks/whatsapp`, with `META_VERIFY_TOKEN` and `META_APP_SECRET`. Configure a carrier callback at `POST /webhooks/momo` with the `X-KaziFlow-Callback-Secret` header. The carrier callback must echo the `merchant_reference` issued by the engine.

## Production notes

- Set a base64-encoded 32-byte `PII_ENCRYPTION_KEY`; production will not start without one.
- Use an HTTPS ingress/WAF in front of the API. The application authenticates Meta payloads but does not replace network DDoS protection.
- Configure actual provider URLs and secrets only through a secrets manager/environment. The checked-in `.env.example` contains no credentials. MTN uses Request-to-Pay fields; Airtel and Daraja adapters expose their canonical request boundary and can be specialized for a merchant agreement’s authentication/signing procedure.
- Deploy a single consumer for each inbound stream shard (the supplied compose file starts one). This preserves in-order per-customer FSM handling. Redis customer locks protect against accidental overlap during a rollout.
- Run database migrations with `alembic upgrade head` (the API and worker entry point do this automatically).

## Deploy to Render

This project is ready to deploy on Render with the included Dockerfile. Render exposes the application port through the `PORT` environment variable, so the container entrypoint is configured to honor it automatically.

1. Push the repository to GitHub.
2. In Render, create a new Web Service and connect the repo.
3. Use the Dockerfile-based build.
4. In the service environment, set the production variables from `.env.example` and replace the sample values with real secrets.
5. Add PostgreSQL and Redis add-ons from Render and copy their URLs into `DATABASE_URL` and `REDIS_URL`.
6. Set the app health check to `/healthz`.
7. Use the public web URL as the Meta webhook URL, for example `https://<your-render-app>.onrender.com/webhooks/whatsapp`.

Example Render runtime command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

The app is already validated to start successfully in production-style mode and to pass the security and merchant-creation checks.

## Important workflow behavior

| Condition | System behavior |
| --- | --- |
| Duplicate Meta message | Redis `SET NX EX 86400` acknowledges and drops it. |
| STK unavailable/timeout | The customer gets retry, manual-till, and cash fallback choices. |
| Two customers select a slot | First Redis holder wins for 10 minutes; permanent booking uses a database row lock. |
| Merchant changes FX rate | Existing checkout orders keep their locked exchange rate and lines. |
| Delivery pin is outside radius | The bot offers pickup or a custom courier quote path. |
| Manual payment claimed | Merchant dispatch channel receives approve/reject buttons. |

## Tests

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check app tests migrations
```
