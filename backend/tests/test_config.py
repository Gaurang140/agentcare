"""Configuration invariants that must fail before a production app boots."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "secret",
    ["", "short", "change_me_generate_a_long_random_string"],
)
def test_non_development_environment_rejects_unsafe_jwt_secret(secret):
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            _env_file=None,
            environment="prod",
            jwt_secret=secret,
            database_url="postgresql+psycopg://agentcare:test@db/agentcare",
        )


def test_non_development_environment_accepts_random_length_jwt_secret():
    settings = Settings(
        _env_file=None,
        environment="prod",
        jwt_secret="a" * 64,
        database_url="postgresql+psycopg://agentcare:test@db/agentcare",
    )

    assert settings.jwt_secret == "a" * 64


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:///./agentcare.db", "sqlite+pysqlite:///:memory:"],
)
def test_non_development_environment_rejects_sqlite(database_url):
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="a" * 64,
            database_url=database_url,
        )


def test_development_can_boot_before_local_secret_is_configured():
    settings = Settings(
        _env_file=None,
        environment="dev",
        jwt_secret="",
    )

    assert settings.jwt_secret == ""


def test_langfuse_sampling_is_disabled_by_default():
    settings = Settings(_env_file=None)

    assert settings.langfuse_sample_rate == 0


@pytest.mark.parametrize("sample_rate", [-0.01, 1.01])
def test_langfuse_sampling_rejects_values_outside_zero_to_one(sample_rate):
    with pytest.raises(ValidationError, match="langfuse_sample_rate"):
        Settings(_env_file=None, langfuse_sample_rate=sample_rate)


def test_langfuse_uses_official_base_url_environment_name(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://eu.cloud.langfuse.com")

    settings = Settings(_env_file=None)

    assert settings.langfuse_base_url == "https://eu.cloud.langfuse.com"
