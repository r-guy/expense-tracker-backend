"""
Currency-rate backend for the Personal Finance Manager app.

A small FastAPI service that proxies ExchangeRate-API v6, keeps the API key
server-side, and caches responses in memory so the free-tier quota is not
burned by every app refresh.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000

Run with Docker:
    docker compose up --build -d
"""

from __future__ import annotations

import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()

API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "").strip()
API_BASE = os.getenv(
    "EXCHANGE_RATE_API_BASE", "https://v6.exchangerate-api.com/v6"
).rstrip("/")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "10"))
PORT = int(os.getenv("PORT", "8000"))


class UpstreamError(RuntimeError):
    """Raised when ExchangeRate-API cannot be reached or answers with an error."""


# base currency -> (fetched_at_epoch, payload)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS),
        headers={"User-Agent": "ExpenseTrackerCurrencyBackend/1.0"},
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Expense Tracker Currency Backend",
    description="Cached proxy for ExchangeRate-API v6.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling: always answer with {"result": "error", "error": "..."} so the
# Android client can use the same parser for success and failure bodies.
# ---------------------------------------------------------------------------


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={"result": "error", "error": message},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, _exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"result": "error", "error": "Invalid request parameters"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_currency(value: str | None, field: str = "base") -> str:
    code = (value or "USD").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"{field} must be a 3-letter currency code, e.g. USD")
    return code


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"result": "error", "error": message},
    )


async def _fetch_upstream(base: str) -> dict[str, Any]:
    """Fetch the latest rates from ExchangeRate-API."""
    if not API_KEY:
        raise UpstreamError("EXCHANGE_RATE_API_KEY is not configured")

    url = f"{API_BASE}/{API_KEY}/latest/{base}"
    client: httpx.AsyncClient = app.state.http

    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach ExchangeRate-API: {exc}") from exc

    if response.status_code != 200:
        raise UpstreamError(
            f"ExchangeRate-API returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise UpstreamError("ExchangeRate-API returned invalid JSON") from exc

    if data.get("result") != "success":
        raise UpstreamError(
            f"ExchangeRate-API error: {data.get('error-type', 'unknown error')}"
        )

    return {
        "result": "success",
        "base_code": data.get("base_code", base),
        "time_last_update_unix": data.get("time_last_update_unix"),
        "time_last_update_utc": data.get("time_last_update_utc"),
        "time_next_update_unix": data.get("time_next_update_unix"),
        "time_next_update_utc": data.get("time_next_update_utc"),
        "conversion_rates": data.get("conversion_rates", {}),
    }


async def _get_rates(base: str) -> tuple[dict[str, Any], bool, bool]:
    """Return (payload, from_cache, is_stale)."""
    now = time.time()
    cached = _cache.get(base)

    if cached is not None and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1], True, False

    try:
        payload = await _fetch_upstream(base)
    except UpstreamError:
        # Serve slightly outdated data rather than failing, if we have any.
        if cached is not None:
            return cached[1], True, True
        raise

    _cache[base] = (time.time(), payload)
    return payload, False, False


def _rates_response(
    payload: dict[str, Any], from_cache: bool, is_stale: bool
) -> dict[str, Any]:
    body = dict(payload)
    body["cached"] = from_cache
    if is_stale:
        body["stale"] = True
    return body


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    return {
        "service": "expense-tracker-currency-backend",
        "docs": "/docs",
        "endpoints": [
            "/health",
            "/api/rates/latest?base=USD",
            "/api/rates/latest/{base}",
            "/api/convert?amount=1&from=USD&to=EUR",
        ],
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "cache_entries": len(_cache),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }


@app.get("/api/rates/latest")
async def latest_rates(base: str = Query("USD", description="Base currency code")):
    try:
        code = _normalize_currency(base)
    except ValueError as exc:
        return _error(400, str(exc))

    try:
        payload, from_cache, is_stale = await _get_rates(code)
    except UpstreamError as exc:
        return _error(502, str(exc))

    return _rates_response(payload, from_cache, is_stale)


@app.get("/api/rates/latest/{base}")
async def latest_rates_for_base(base: str):
    try:
        code = _normalize_currency(base)
    except ValueError as exc:
        return _error(400, str(exc))

    try:
        payload, from_cache, is_stale = await _get_rates(code)
    except UpstreamError as exc:
        return _error(502, str(exc))

    return _rates_response(payload, from_cache, is_stale)


@app.get("/api/convert")
async def convert(
    amount: float = Query(1.0, description="Amount to convert"),
    from_currency: str = Query("USD", alias="from", description="Source currency"),
    to_currency: str = Query("USD", alias="to", description="Target currency"),
):
    if not math.isfinite(amount):
        return _error(400, "amount must be a finite number")

    try:
        source = _normalize_currency(from_currency, "from")
        target = _normalize_currency(to_currency, "to")
    except ValueError as exc:
        return _error(400, str(exc))

    # ExchangeRate-API free tier is USD-based; convert through USD for any pair.
    try:
        payload, from_cache, is_stale = await _get_rates("USD")
    except UpstreamError as exc:
        return _error(502, str(exc))

    rates = payload.get("conversion_rates") or {}
    if source not in rates or target not in rates:
        return _error(400, f"Unsupported currency code: {source}->{target}")

    rate = rates[target] / rates[source]
    body: dict[str, Any] = {
        "result": "success",
        "amount": amount,
        "from": source,
        "to": target,
        "rate": rate,
        "converted": amount * rate,
        "time_last_update_utc": payload.get("time_last_update_utc"),
        "cached": from_cache,
    }
    if is_stale:
        body["stale"] = True
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
