"""Server-only, expiring wallet challenges on the existing Redis instance."""

import hashlib
from functools import lru_cache

from redis import Redis

from app.core.config import get_settings


CONSUME = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""
RATE_LIMIT = """
local allowed = 1
for i, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    if count == 1 then redis.call('EXPIRE', key, ARGV[1]) end
    if count > tonumber(ARGV[i + 1]) then allowed = 0 end
end
return allowed
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WalletChallengeStore:
    def __init__(self, client: Redis):
        self.client = client

    @staticmethod
    def key(challenge_id: str) -> str:
        return f"screener:auth:wallet:challenge:{challenge_id}"

    def save(self, challenge_id: str, raw: str, ttl: int) -> None:
        if not self.client.set(self.key(challenge_id), raw, nx=True, ex=ttl):
            raise RuntimeError("Wallet challenge collision")

    def read(self, challenge_id: str) -> str | None:
        return self.client.get(self.key(challenge_id))

    def consume(self, challenge_id: str, raw: str) -> bool:
        return bool(self.client.eval(CONSUME, 1, self.key(challenge_id), raw))

    def rate_limit(self, bucket: str, subjects: list[tuple[str, str, int]], window: int = 300) -> bool:
        keys = [f"screener:auth:wallet:rate:{bucket}:{kind}:{digest(value)}" for kind, value, _ in subjects]
        return bool(self.client.eval(RATE_LIMIT, len(keys), *keys, window, *(limit for _, _, limit in subjects)))


@lru_cache
def get_wallet_challenge_store() -> WalletChallengeStore:
    return WalletChallengeStore(Redis.from_url(
        get_settings().agent_redis_url, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3, health_check_interval=30,
    ))
