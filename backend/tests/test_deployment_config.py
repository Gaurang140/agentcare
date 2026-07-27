"""Regression tests for the committed Compose and Kubernetes deployment config."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _resource(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"{kind}/{name} is not declared")


def test_compose_uses_documented_env_files_and_requires_operator_jwt():
    compose = _load_yaml(REPO_ROOT / "docker-compose.yml")
    backend = compose["services"]["backend"]

    env_files = backend.get("env_file")
    assert env_files == [
        ".env.example",
        {"path": ".env", "required": False},
    ]

    environment = backend["environment"]
    assert environment["DATABASE_URL"] == (
        "postgresql+psycopg://agentcare:agentcare@db:5432/agentcare"
    )
    assert environment["FRONTEND_ORIGIN"] == "http://localhost:3000"
    assert environment["JWT_SECRET"].startswith("${JWT_SECRET:?")
    assert "LLM_MODEL" not in environment
    assert "LLM_BASE_URL" not in environment


def test_kubernetes_base_selects_profile_without_field_overrides():
    configmap = _load_yaml(REPO_ROOT / "infra/k8s/base/configmap.yaml")
    data = configmap["data"]

    assert data.get("LLM_PROFILE") == "groq"
    assert "LLM_MODEL" not in data
    assert "LLM_BASE_URL" not in data


def test_kubernetes_backend_uses_declared_service_account_and_skips_migrations():
    base = _load_yaml(REPO_ROOT / "infra/k8s/base/kustomization.yaml")
    assert "service-account.yaml" in base["resources"]

    documents = list(
        yaml.safe_load_all(
            (REPO_ROOT / "infra/k8s/base/backend.yaml").read_text(encoding="utf-8")
        )
    )
    deployment = _resource(documents, "Deployment", "backend")
    pod_spec = deployment["spec"]["template"]["spec"]

    assert pod_spec["serviceAccountName"] == "agentcare-backend"
    container = _resource(
        [
            {
                "kind": "Container",
                "metadata": {"name": item["name"]},
                **item,
            }
            for item in pod_spec["containers"]
        ],
        "Container",
        "backend",
    )
    env = {item["name"]: item["value"] for item in container.get("env", [])}
    assert env["SKIP_STARTUP_MIGRATIONS"] == "true"


def test_application_base_excludes_migration_job():
    base = _load_yaml(REPO_ROOT / "infra/k8s/base/kustomization.yaml")

    assert "migration-job.yaml" not in base["resources"]


def test_gcp_migration_overlay_owns_job_and_backend_image():
    overlay_path = REPO_ROOT / "infra/k8s/overlays/gcp-migration/kustomization.yaml"
    assert overlay_path.is_file()

    overlay = _load_yaml(overlay_path)
    assert "migration-job.yaml" in overlay["resources"]
    assert overlay["images"] == [
        {
            "name": "agentcare-backend",
            "newName": "REGION-docker.pkg.dev/PROJECT/agentcare/backend",
            "newTag": "TAG",
        }
    ]
