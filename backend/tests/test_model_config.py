"""TDD for app.agents.model_config: YAML model profiles with env override.

backend/llm.yaml names provider profiles for the LangChain chat-model
factory. The rules under test:

1. The committed llm.yaml is the default source of profile values.
2. Environment variables always win over the file (AGENTS.md: config via
   environment) - but only when actually set, not via their defaults.
3. A missing or malformed file degrades to env/default settings with a
   warning, never an exception: a bad YAML edit must not take the app down.
"""

from __future__ import annotations

from pathlib import Path

from app.agents.model_config import ModelProfile, load_llm_profiles
from app.config import Settings


def _settings(monkeypatch, **env: str) -> Settings:
    """A Settings instance built from exactly the given env vars, with the
    repo .env file ignored so tests stay hermetic."""
    for key in (
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_PROFILE",
        "LLM_FALLBACK_BASE_URL",
        "LLM_FALLBACK_MODEL",
        "LLM_FALLBACK_API_KEY",
        "INJECTION_GUARD_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_committed_yaml_provides_groq_default_profile(monkeypatch):
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings)

    assert profiles.primary.provider == "openai"
    assert profiles.primary.model == "openai/gpt-oss-120b"
    assert profiles.primary.base_url == "https://api.groq.com/openai/v1"
    assert profiles.primary.timeout == 30
    assert profiles.primary.max_retries == 3
    assert profiles.injection_guard_model == "meta-llama/llama-prompt-guard-2-86m"


def test_committed_yaml_provides_vertex_profile(monkeypatch):
    settings = _settings(monkeypatch, LLM_PROFILE="vertex")

    profiles = load_llm_profiles(settings)

    assert profiles.primary.provider == "google_genai"
    assert profiles.primary.model == "gemini-2.5-flash"
    assert profiles.primary.params == {"vertexai": True}


def test_env_model_overrides_yaml(monkeypatch):
    settings = _settings(monkeypatch, LLM_MODEL="my-custom-model")

    profiles = load_llm_profiles(settings)

    assert profiles.primary.model == "my-custom-model"
    # the rest still comes from the yaml profile
    assert profiles.primary.base_url == "https://api.groq.com/openai/v1"


def test_llm_profile_env_selects_named_profile(monkeypatch, tmp_path: Path):
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: first
profiles:
  first:
    provider: openai
    model: model-one
    base_url: https://one.test/v1
  second:
    provider: google_genai
    model: gemini-2.5-flash
    location: europe-west3
    timeout: 12
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch, LLM_PROFILE="second")

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.primary.provider == "google_genai"
    assert profiles.primary.model == "gemini-2.5-flash"
    assert profiles.primary.timeout == 12
    # provider-specific extras ride along for init_chat_model
    assert profiles.primary.params == {"location": "europe-west3"}


def test_unknown_profile_name_falls_back_to_default_profile(monkeypatch, tmp_path: Path):
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: first
profiles:
  first:
    provider: openai
    model: model-one
    base_url: https://one.test/v1
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch, LLM_PROFILE="does-not-exist")

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.primary.model == "model-one"


def test_missing_file_degrades_to_env_settings(monkeypatch, tmp_path: Path):
    settings = _settings(monkeypatch, LLM_MODEL="env-model", LLM_BASE_URL="https://env.test/v1")

    profiles = load_llm_profiles(settings, path=tmp_path / "nope.yaml")

    assert profiles.primary.provider == "openai"
    assert profiles.primary.model == "env-model"
    assert profiles.primary.base_url == "https://env.test/v1"


def test_malformed_yaml_degrades_to_env_settings(monkeypatch, tmp_path: Path):
    config = tmp_path / "llm.yaml"
    config.write_text("profiles: [unclosed", encoding="utf-8")
    settings = _settings(monkeypatch, LLM_MODEL="env-model")

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.primary.model == "env-model"


def test_no_fallback_by_default(monkeypatch):
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings)

    assert profiles.fallback is None


def test_env_fallback_endpoint_builds_fallback_profile(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_FALLBACK_BASE_URL="http://localhost:1234/v1",
        LLM_FALLBACK_MODEL="local-model",
    )

    profiles = load_llm_profiles(settings)

    assert profiles.fallback == ModelProfile(
        provider="openai",
        model="local-model",
        base_url="http://localhost:1234/v1",
    )


def test_yaml_fallback_profile_used_when_env_has_none(monkeypatch, tmp_path: Path):
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: first
fallback_profile: local
profiles:
  first:
    provider: openai
    model: model-one
    base_url: https://one.test/v1
  local:
    provider: openai
    model: local-model
    base_url: http://localhost:8080/v1
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.fallback is not None
    assert profiles.fallback.model == "local-model"
    assert profiles.fallback.base_url == "http://localhost:8080/v1"


def test_injection_guard_env_overrides_yaml(monkeypatch):
    settings = _settings(monkeypatch, INJECTION_GUARD_MODEL="my-guard")

    profiles = load_llm_profiles(settings)

    assert profiles.injection_guard_model == "my-guard"


def test_malformed_profile_value_degrades_to_env_settings(monkeypatch, tmp_path: Path):
    """A profile with a non-numeric timeout must cost a warning, not a 500
    on every request."""
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: broken
profiles:
  broken:
    provider: openai
    model: some-model
    timeout: not-a-number
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch, LLM_MODEL="env-model")

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.primary.model == "env-model"


def test_guard_uses_named_profile_when_configured(monkeypatch, tmp_path: Path):
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: main
profiles:
  main:
    provider: google_genai
    model: gemini-2.5-flash
  guard-endpoint:
    provider: openai
    model: unused
    base_url: https://guard.test/v1
injection_guard:
  model: my-guard-model
  profile: guard-endpoint
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.guard is not None
    assert profiles.guard.base_url == "https://guard.test/v1"
    assert profiles.injection_guard_model == "my-guard-model"


def test_guard_defaults_to_primary_when_openai_compatible(monkeypatch):
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings)

    assert profiles.guard == profiles.primary


def test_guard_disabled_when_primary_is_not_openai_compatible(monkeypatch, tmp_path: Path):
    """A prompt-guard model name from one provider must never be sent to a
    different provider's endpoint just because it is the primary."""
    config = tmp_path / "llm.yaml"
    config.write_text(
        """
default_profile: main
profiles:
  main:
    provider: google_genai
    model: gemini-2.5-flash
""",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch)

    profiles = load_llm_profiles(settings, path=config)

    assert profiles.guard is None
