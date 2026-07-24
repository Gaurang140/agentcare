"""Application configuration loaded from environment variables / .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root .env, resolved independent of the process working directory.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    # LLM (OpenAI-compatible)
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"

    # Prompt-injection guard's optional classifier layer
    # (safety/injection_guard.py): a small preview model that reviews text
    # the deterministic pattern list alone would miss. Only called when
    # llm_api_key is also set; empty runs the deterministic layer alone.
    injection_guard_model: str = "meta-llama/llama-prompt-guard-2-86m"

    # LLM fallback endpoint (optional; e.g. a local LM Studio server). Used
    # only when the primary client exhausts its retries.
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    # Database
    database_url: str = "sqlite:///./agentcare.db"

    # LangGraph checkpointer: a raw sqlite3 file path (not a SQLAlchemy URL),
    # kept separate from the app db to avoid file-locking conflicts between
    # the two. Ignored when database_url is postgres (checkpoints then live
    # in the same database).
    checkpoint_db_path: str = "checkpoints.db"

    # Langfuse tracing (optional, env-gated): entirely inert while both keys
    # are empty (the default). Enabling it also requires `pip install
    # langchain`, since langfuse.langchain.CallbackHandler imports it - not
    # pinned in requirements.txt because the default path never needs it.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    # Auth
    jwt_secret: str = "change_me_generate_a_long_random_string"
    jwt_expire_minutes: int = 1440

    # Internal task token: when set, POST /api/internal/reminders/run-due
    # accepts an X-Internal-Token header equal to this value instead of a
    # staff cookie (for cron-style callers with no browser session). Empty
    # (the default) means that route falls back to require_role("staff").
    internal_task_token: str = ""

    # Storage: local | gcs
    storage_backend: str = "local"
    upload_dir: str = "./uploads"
    gcs_bucket: str = ""

    # App
    environment: str = "dev"
    log_level: str = "INFO"
    frontend_origin: str = "http://localhost:3000"


settings = Settings()
