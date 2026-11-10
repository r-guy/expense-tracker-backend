# Expense Tracker Backend

FastAPI service for the [Expense Tracker app](https://github.com/r-guy/expense-tracker-app).
It serves cached currency rates; authentication is handled by Neon Auth, not here.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and cache stats |
| GET | `/api/rates/latest?base=USD` | Latest rates for a base currency |
| GET | `/api/rates/latest/{base}` | Same, base in the path |
| GET | `/api/convert?amount=100&from=USD&to=EUR` | Convert between currencies |
| GET | `/docs` | Swagger UI |

Responses mirror ExchangeRate-API's shape plus `cached` / `stale`, so the app
parses backend and direct responses with one parser.

## Configuration (`.env`)

| Variable | Default |
|---|---|
| `EXCHANGE_RATE_API_KEY` | *(required)* |
| `EXCHANGE_RATE_API_BASE` | `https://v6.exchangerate-api.com/v6` |
| `CACHE_TTL_SECONDS` | `3600` |
| `UPSTREAM_TIMEOUT_SECONDS` | `10` |
| `PORT` | `8000` |

Neon values (`DATABASE_URL`, `NEON_AUTH_*`) are written by `neon deploy`.

## Run

```bash
docker compose up --build -d
```

or locally:

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

## Neon Auth

```bash
neon init -y --org-id <org> --project-id <project> --branch production --services "postgres,auth"
neon login
neon deploy
```

## Security

`.env` is git-ignored; `DATABASE_URL` never reaches the Android app. Cleartext
HTTP is only used by debug builds of the app.
