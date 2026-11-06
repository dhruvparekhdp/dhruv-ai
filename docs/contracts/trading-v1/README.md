# trading-v1

Read-only contract between Jarvis and the crypto and NSE engines. Jarvis does
not serve these routes and does not place orders. The engines do.

Base path: `/integration/v1`.

Authentication: `Authorization: Bearer <token>`. The engine compares the token
with `INTEGRATION_API_TOKEN` using a constant-time compare. An empty expected
token fails closed. The Jarvis clients use separate names so the two feeds
cannot share one secret by accident:

- `CRYPTO_INTEGRATION_API_TOKEN` for `crypto-signal-engine`
- `NSE_INTEGRATION_API_TOKEN` for `nse-trade`

Only `GET` is defined:

- `/health`
- `/market/overview`
- `/signals`
- `/positions`
- `/trades`
- `/performance`
- `/risk`
- `/strategies`

Any other method is `405`. There is no order, cancel, amend, withdraw, or
transfer route.

Every response is an envelope. `schema_version` is `1.0`. `mode` is `paper`,
`shadow`, or `research`. There is no live mode. `correlation_id` echoes
`X-Correlation-ID`. `status` is `ok`, `degraded`, or `unavailable`.

List routes (`signals`, `positions`, `trades`, `strategies`) accept `limit`
(1 to 500) and `since`, and return `data.items` plus `data.next_cursor`.

Prices, quantities, and ratios are decimal strings. A JSON number is invalid.
Money is `{amount, currency}`. `change_pct` is a signed fraction (`0.012` means
1.2 percent), not a percent point. `period` is a window with a label and two
timestamps. `params_summary` is a short string, not a map of numbers.

`is_stale: true` means the engine itself considers the payload stale. Clients
also treat `data_as_of` older than their own threshold as stale.

JSON Schemas in this directory are generated from the Pydantic models in
`app/contracts/trading_v1/models.py`. Fixtures under `fixtures/` are examples
for both engines. The Pydantic models are stricter than the schemas on two
points: timestamps must carry a timezone, and `next_cursor` must be null or
non-empty.
