"""Authentication and abuse controls for Telegram-facing application endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    """Raised when Telegram Mini App identity cannot be trusted."""


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("AI rate limit exceeded")
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    username: str | None
    first_name: str
    last_name: str
    auth_date: int


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
    now: int | None = None,
) -> TelegramIdentity:
    """Validate Telegram.WebApp.initData and return its authenticated user.

    This implements Telegram's HMAC-SHA-256 WebAppData procedure and rejects
    duplicate fields, future timestamps, expired sessions, bots, and malformed
    user payloads. The raw initData must never be replaced by initDataUnsafe.
    """
    if not bot_token:
        raise AuthenticationError("Telegram authentication is not configured")
    if not init_data or len(init_data) > 16_384:
        raise AuthenticationError("Missing or oversized Telegram initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise AuthenticationError("Malformed Telegram initData") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise AuthenticationError("Duplicate Telegram initData field")
        values[key] = value
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64:
        raise AuthenticationError("Telegram initData hash is missing")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise AuthenticationError("Telegram initData signature is invalid")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Telegram initData user is invalid") from exc
    timestamp = int(time.time()) if now is None else int(now)
    if auth_date > timestamp + 30:
        raise AuthenticationError("Telegram initData timestamp is in the future")
    if max_age_seconds > 0 and timestamp - auth_date > max_age_seconds:
        raise AuthenticationError("Telegram session has expired; reopen the Mini App")
    if user_id <= 0 or user.get("is_bot") is True:
        raise AuthenticationError("Telegram user is invalid")
    return TelegramIdentity(
        user_id=user_id,
        username=str(user.get("username") or "").strip() or None,
        first_name=str(user.get("first_name") or "").strip(),
        last_name=str(user.get("last_name") or "").strip(),
        auth_date=auth_date,
    )


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    """Fixed-window limiter backed by Postgres, with an explicit local fallback."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = (database_url or "").strip()
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[int, int]] = {}

    def consume(self, subject: str, scope: str, limit: int, window_seconds: int) -> RateLimitResult:
        if limit <= 0 or window_seconds <= 0:
            return RateLimitResult(True, 2**31 - 1, 0)
        if self.database_url:
            return self._consume_postgres(subject, scope, limit, window_seconds)
        return self._consume_local(subject, scope, limit, window_seconds)

    def _consume_local(self, subject: str, scope: str, limit: int,
                       window_seconds: int) -> RateLimitResult:
        current = int(time.time())
        key = (subject, scope)
        with self._lock:
            started, count = self._buckets.get(key, (current, 0))
            if current - started >= window_seconds:
                started, count = current, 0
            if count >= limit:
                return RateLimitResult(False, 0, max(1, window_seconds - (current - started)))
            count += 1
            self._buckets[key] = (started, count)
            return RateLimitResult(True, max(0, limit - count), 0)

    def _consume_postgres(self, subject: str, scope: str, limit: int,
                          window_seconds: int) -> RateLimitResult:
        import psycopg

        now = datetime.now(timezone.utc)
        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                # Serialize even the first insert for a bucket; SELECT FOR UPDATE
                # alone cannot lock a row that does not exist yet.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s || ':' || %s, 0))",
                    (subject, scope),
                )
                cursor.execute(
                    "SELECT window_started_at,request_count FROM public.rate_limit_buckets "
                    "WHERE subject_key=%s AND scope=%s FOR UPDATE",
                    (subject, scope),
                )
                row = cursor.fetchone()
                if row is None or (now - row[0]).total_seconds() >= window_seconds:
                    cursor.execute(
                        "INSERT INTO public.rate_limit_buckets(subject_key,scope,window_started_at,request_count) "
                        "VALUES(%s,%s,%s,1) ON CONFLICT(subject_key,scope) DO UPDATE SET "
                        "window_started_at=excluded.window_started_at,request_count=1",
                        (subject, scope, now),
                    )
                    return RateLimitResult(True, max(0, limit - 1), 0)
                count = int(row[1])
                if count >= limit:
                    elapsed = int((now - row[0]).total_seconds())
                    return RateLimitResult(False, 0, max(1, window_seconds - elapsed))
                cursor.execute(
                    "UPDATE public.rate_limit_buckets SET request_count=request_count+1 "
                    "WHERE subject_key=%s AND scope=%s", (subject, scope),
                )
                return RateLimitResult(True, max(0, limit - count - 1), 0)


def configured_rate_limiter() -> RateLimiter:
    return RateLimiter(os.getenv("PAPER_DATABASE_URL") or os.getenv("SUPABASE_DB_URL"))
