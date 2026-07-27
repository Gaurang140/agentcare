"""YAML model profiles for the LangChain chat-model factory in `llm.py`.

`backend/llm.yaml` names provider profiles (Groq, a local server, Vertex
AI, ...). Environment variables always win over the file - `LLM_MODEL`,
`LLM_BASE_URL` and friends override fields of the active profile, and
`LLM_PROFILE` picks a profile by name - so config stays env-driven
(AGENTS.md) and the YAML is the editable layer of defaults underneath.

The file is read on every call, like the staff-editable agent rules, so an
edit applies on the next request. Any read or parse problem degrades to
the env/default settings with a logged warning: a bad YAML edit can never
take the backend down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import Settings, settings as global_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

# backend/llm.yaml, resolved relative to this file so it works no matter
# the process working directory (uvicorn runs from backend/, compose from /app).
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "llm.yaml"

# Profile keys with dedicated fields; everything else a profile carries is
# passed through to init_chat_model untouched (e.g. `location`,
# `temperature`, `project`), so provider-specific knobs need no code change.
_KNOWN_KEYS = {"provider", "model", "base_url", "timeout", "max_retries"}


@dataclass(frozen=True)
class ModelProfile:
    """One named entry under `profiles:` in llm.yaml."""

    provider: str = "openai"
    model: str = ""
    base_url: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMProfiles:
    """What `llm.py` consumes: the resolved primary model, the optional
    fallback tried after transport exhaustion, and the guard model name."""

    primary: ModelProfile
    fallback: ModelProfile | None
    injection_guard_model: str


def _profile_from_mapping(raw: dict) -> ModelProfile:
    params = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}
    return ModelProfile(
        provider=str(raw.get("provider", "openai")),
        model=str(raw.get("model", "")),
        base_url=raw.get("base_url"),
        timeout=float(raw.get("timeout", 30.0)),
        max_retries=int(raw.get("max_retries", 3)),
        params=params,
    )


def _read_yaml(path: Path) -> dict:
    """Parse llm.yaml, returning {} for anything unusable. The try block is
    deliberately wide: an unreadable or malformed file must only cost a
    warning, never a request."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning("llm_yaml_missing_using_env_defaults", path=str(path))
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("llm_yaml_unreadable_using_env_defaults", path=str(path), error=str(exc))
        return {}
    if not isinstance(data, dict):
        logger.warning("llm_yaml_not_a_mapping_using_env_defaults", path=str(path))
        return {}
    return data


def _select_profile(data: dict, requested: str) -> dict:
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return {}
    name = requested or str(data.get("default_profile", ""))
    if name not in profiles:
        if requested:
            logger.warning("llm_profile_unknown_using_default", requested=requested)
        name = str(data.get("default_profile", ""))
    raw = profiles.get(name)
    return raw if isinstance(raw, dict) else {}


def _is_env_set(settings: Settings, field_name: str) -> bool:
    """True when the field was provided via env/.env rather than left at
    its class default - the signal that env should override the YAML."""
    return field_name in settings.model_fields_set


def load_llm_profiles(
    settings: Settings | None = None, path: Path | None = None
) -> LLMProfiles:
    """Resolve the model configuration `llm.py` should use right now."""
    settings = settings if settings is not None else global_settings
    data = _read_yaml(path if path is not None else _DEFAULT_PATH)

    raw_primary = _select_profile(data, settings.llm_profile)
    primary = _profile_from_mapping(raw_primary) if raw_primary else ModelProfile()

    # Env overrides, applied field-by-field only where env actually set a
    # value. With no YAML at all this also reconstructs today's env-only
    # behavior, because the Settings defaults are the Groq endpoint.
    overrides: dict = {}
    if not raw_primary or _is_env_set(settings, "llm_model"):
        overrides["model"] = settings.llm_model
    if not raw_primary or _is_env_set(settings, "llm_base_url"):
        overrides["base_url"] = settings.llm_base_url
    if overrides:
        primary = ModelProfile(
            provider=primary.provider,
            model=overrides.get("model", primary.model),
            base_url=overrides.get("base_url", primary.base_url),
            timeout=primary.timeout,
            max_retries=primary.max_retries,
            params=primary.params,
        )

    # Fallback: the env endpoint wins outright (exact pre-YAML semantics);
    # otherwise an optional named profile from the file.
    fallback: ModelProfile | None = None
    if settings.llm_fallback_base_url:
        fallback = ModelProfile(
            provider="openai",
            model=settings.llm_fallback_model,
            base_url=settings.llm_fallback_base_url,
        )
    else:
        fallback_name = str(data.get("fallback_profile") or "")
        raw_fallback = data.get("profiles", {}).get(fallback_name) if fallback_name else None
        if isinstance(raw_fallback, dict):
            fallback = _profile_from_mapping(raw_fallback)

    guard = data.get("injection_guard", {})
    guard_model = str(guard.get("model", "")) if isinstance(guard, dict) else ""
    if not guard_model or _is_env_set(settings, "injection_guard_model"):
        guard_model = settings.injection_guard_model

    return LLMProfiles(primary=primary, fallback=fallback, injection_guard_model=guard_model)
