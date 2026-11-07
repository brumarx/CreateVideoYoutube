"""Rodízio de chaves e cooldown de 429 (correções de 06/11: Groq queimava a
cota da 1ª chave porque o rotador era recriado a cada chamada)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import providers  # noqa: E402
from src.providers import KeyRotator, _cooldown_seconds, _rotator_for  # noqa: E402


def test_same_key_list_returns_same_rotator():
    keys = ["k1", "k2", "k3"]
    assert _rotator_for(list(keys)) is _rotator_for(list(keys))


def test_order_rotates_first_key():
    r = KeyRotator(["a", "b", "c"])
    firsts = [r.order()[0] for _ in range(6)]
    assert firsts == ["a", "b", "c", "a", "b", "c"]


def test_rest_removes_key_temporarily():
    r = KeyRotator(["a", "b"])
    r.rest("a", 60)
    assert r.order() == ["b"]


def test_ban_removes_key_for_good():
    r = KeyRotator(["a", "b"])
    r.ban("a")
    for _ in range(3):
        assert "a" not in r.order()


def test_all_banned_gives_empty_order():
    r = KeyRotator(["a"])
    r.ban("a")
    assert r.order() == []


def test_cooldown_tpm_is_short():
    assert _cooldown_seconds(429, "Rate limit reached (TPM): Limit 8000") == 75


def test_cooldown_tpd_waits_until_utc_reset():
    wait = _cooldown_seconds(429, "Rate limit reached (TPD) per day")
    assert 60 <= wait <= 86400 + 300


def test_cooldown_only_for_429():
    assert _cooldown_seconds(401, "invalid key") == 0
    assert _cooldown_seconds(500, "server") == 0
