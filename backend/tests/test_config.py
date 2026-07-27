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
        )


def test_non_development_environment_accepts_random_length_jwt_secret():
    settings = Settings(
        _env_file=None,
        environment="prod",
        jwt_secret="a" * 64,
    )

    assert settings.jwt_secret == "a" * 64


def test_development_can_boot_before_local_secret_is_configured():
    settings = Settings(
        _env_file=None,
        environment="dev",
        jwt_secret="",
    )

    assert settings.jwt_secret == ""
