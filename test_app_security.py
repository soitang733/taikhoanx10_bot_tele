"""Security regression tests for Telegram identity and AI throttling."""

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from app_security import AuthenticationError, RateLimiter, validate_telegram_init_data


TOKEN = "123456:test-token"


def signed_init_data(user_id: int = 42, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAE-test",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": "tester"},
                           ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TelegramAuthenticationTests(unittest.TestCase):
    def test_accepts_valid_signed_user(self):
        identity = validate_telegram_init_data(signed_init_data(), TOKEN)
        self.assertEqual(identity.user_id, 42)
        self.assertEqual(identity.username, "tester")

    def test_rejects_tampering(self):
        payload = signed_init_data().replace("tester", "attacker")
        with self.assertRaisesRegex(AuthenticationError, "signature"):
            validate_telegram_init_data(payload, TOKEN)

    def test_rejects_expired_payload(self):
        now = int(time.time())
        with self.assertRaisesRegex(AuthenticationError, "expired"):
            validate_telegram_init_data(signed_init_data(auth_date=now - 101), TOKEN,
                                        max_age_seconds=100, now=now)

    def test_rejects_duplicate_fields(self):
        with self.assertRaisesRegex(AuthenticationError, "Duplicate"):
            validate_telegram_init_data(signed_init_data() + "&auth_date=1", TOKEN)


class RateLimiterTests(unittest.TestCase):
    def test_limits_each_subject_independently(self):
        limiter = RateLimiter()
        self.assertTrue(limiter.consume("tg:1", "ai", 2, 60).allowed)
        self.assertTrue(limiter.consume("tg:1", "ai", 2, 60).allowed)
        blocked = limiter.consume("tg:1", "ai", 2, 60)
        self.assertFalse(blocked.allowed)
        self.assertGreaterEqual(blocked.retry_after, 1)
        self.assertTrue(limiter.consume("tg:2", "ai", 2, 60).allowed)


if __name__ == "__main__":
    unittest.main()
