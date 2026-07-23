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

    # Database
    database_url: str = "sqlite:///./agentcare.db"

    # Auth
    jwt_secret: str = "change_me_generate_a_long_random_string"
    jwt_expire_minutes: int = 1440

    # Storage: local | gcs
    storage_backend: str = "local"
    upload_dir: str = "./uploads"
    gcs_bucket: str = ""

    # App
    environment: str = "dev"
    log_level: str = "INFO"
    frontend_origin: str = "http://localhost:3000"


settings = Settings()
