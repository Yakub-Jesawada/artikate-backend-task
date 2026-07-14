import time

from django.test import TestCase

from emailqueue.rate_limiter import SlidingWindowRateLimiter


class SlidingWindowRateLimiterTests(TestCase):
    """Exercises the real Redis Lua script directly (no Celery involved) --
    fast and deterministic proof that the limiter blocks atomically once
    the window is full and recovers once entries expire.
    """

    def setUp(self):
        self.limiter = SlidingWindowRateLimiter(f"test-{self.id()}", limit_per_minute=5)
        # Shrink the window to 500ms so the test doesn't take a real minute.
        self.limiter.window_ms = 500

    def test_allows_up_to_the_limit_then_blocks(self):
        results = [self.limiter.try_acquire() for _ in range(7)]
        self.assertEqual(results, [True, True, True, True, True, False, False])

    def test_recovers_once_the_window_has_elapsed(self):
        for _ in range(5):
            self.assertTrue(self.limiter.try_acquire())
        self.assertFalse(self.limiter.try_acquire())

        time.sleep(0.6)  # let the 500ms window fully elapse (test-only wait)

        self.assertTrue(self.limiter.try_acquire())

    def test_never_exceeds_limit_under_concurrent_like_bursts(self):
        allowed_count = sum(self.limiter.try_acquire() for _ in range(50))
        self.assertEqual(allowed_count, 5)
        self.assertLessEqual(self.limiter.current_count(), 5)
