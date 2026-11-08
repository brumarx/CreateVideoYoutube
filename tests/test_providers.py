"""Rodízio de chaves e cooldown de 429 (correções de 06/11: Groq queimava a
cota da 1ª chave porque o rotador era recriado a cada chamada)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src import providers  # noqa: E402
from src.providers import KeyRotator, _cooldown_seconds, _rotator_for  # noqa: E402


@pytest.fixture(autouse=True)
def _saude_temporaria(tmp_path, monkeypatch):
    # o estado de saúde não pode sujar o arquivo real nos testes
    monkeypatch.setattr(providers, "HEALTH_FILE", tmp_path / "provider_health.json")


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


def test_ban_survives_a_new_process(tmp_path):
    # a chave banida por um job continua banida pro próximo (estado em disco)
    r1 = KeyRotator(["ka", "kb"])
    r1.ban("ka")
    r2 = KeyRotator(["ka", "kb"])  # simula outro processo
    for _ in range(3):
        assert "ka" not in r2.order()


def test_rest_survives_a_new_process():
    r1 = KeyRotator(["ka", "kb"])
    r1.rest("ka", 300)
    r2 = KeyRotator(["ka", "kb"])
    assert r2.order() == ["kb"]


def test_expired_rest_is_forgotten():
    import json as _json

    r1 = KeyRotator(["ka", "kb"])
    r1.rest("ka", 300)
    data = _json.loads(providers.HEALTH_FILE.read_text())
    for v in data.values():
        v["until"] = 1.0  # já passou
    providers.HEALTH_FILE.write_text(_json.dumps(data))
    assert "ka" in KeyRotator(["ka", "kb"]).order()


def test_health_file_does_not_store_raw_key():
    r = KeyRotator(["segredo-123"])
    r.ban("segredo-123")
    assert "segredo-123" not in providers.HEALTH_FILE.read_text()


def test_health_reset_clears_bans_and_rests():
    r = KeyRotator(["ka", "kb"])
    r.ban("ka")
    r.rest("kb", 300)
    assert providers.health_reset("all") >= 2
    fresh = KeyRotator(["ka", "kb"])
    assert sorted(fresh.order()) == ["ka", "kb"]
