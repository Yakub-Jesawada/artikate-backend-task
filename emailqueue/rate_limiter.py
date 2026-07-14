"""Redis-based sliding-window rate limiter.

See DESIGN.md for why sliding-window-over-sorted-set was chosen over
token-bucket (DECR+TTL) and fixed-window (INCR+EXPIRE), how atomicity is
guaranteed (a single Lua script, executed atomically by Redis), and what
happens on Redis failure (fail closed).
"""

import time
import uuid

import redis
from django.conf import settings

# KEYS[1] = the sorted-set key for this limiter
# ARGV[1] = now, in milliseconds
# ARGV[2] = window size, in milliseconds
# ARGV[3] = max requests allowed in the window
# ARGV[4] = unique member id for this request
#
# All three Redis commands run inside one Lua script, so no other client can
# observe or mutate this key between the ZREMRANGEBYSCORE cleanup, the ZCARD
# read, and the conditional ZADD -- that's the atomicity guarantee (a single
# EVALSHA is executed by Redis as one indivisible operation).
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return 1
else
    return 0
end
"""


class RateLimitExceeded(Exception):
    pass


class RedisUnavailable(Exception):
    """Raised when Redis cannot be reached and the limiter must fail closed."""


class SlidingWindowRateLimiter:
    def __init__(self, name, limit_per_minute=None, redis_client=None):
        self.name = name
        self.limit = limit_per_minute or settings.EMAIL_RATE_LIMIT_PER_MINUTE
        self.window_ms = 60_000
        self._redis = redis_client or redis.from_url(settings.REDIS_URL)
        self._script = self._redis.register_script(_SLIDING_WINDOW_LUA)

    def try_acquire(self):
        """Returns True if the caller may proceed, False if the limit is hit.

        Raises RedisUnavailable on connection failure -- callers decide
        whether that means "block the send" (fail closed, our default here
        because an over-limit send can get the whole account banned by the
        provider) or "let it through" (fail open).
        """
        key = f"ratelimit:{self.name}"
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex}"

        try:
            allowed = self._script(keys=[key], args=[now_ms, self.window_ms, self.limit, member])
        except redis.exceptions.RedisError as exc:
            raise RedisUnavailable(str(exc)) from exc

        return bool(allowed)

    def current_count(self):
        key = f"ratelimit:{self.name}"
        now_ms = int(time.time() * 1000)
        self._redis.zremrangebyscore(key, 0, now_ms - self.window_ms)
        return self._redis.zcard(key)
