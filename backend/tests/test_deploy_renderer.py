"""Deployment rendering must be deterministic and never edit source manifests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
RENDERER = REPO_ROOT / "scripts/render_gcp_manifests.py"


def _load_renderer():
    assert RENDERER.is_file(), "the GCP manifest renderer is missing"
    spec = importlib.util.spec_from_file_location("render_gcp_manifests", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _values(module):
    return module.DeploymentValues(
        project_id="agentcare-prod-123",
        region="europe-west3",
        image_tag="0123456789abcdef0123456789abcdef01234567",
        documents_bucket="agentcare-prod-123-agentcare-documents",
        model_armor_template=(
            "projects/agentcare-prod-123/locations/europe-west3/"
            "templates/agentcare-guard"
        ),
        public_url="https://agentcare.example.com",
        llm_profile="vertex",
        langfuse_public_key="pk-lf-example",
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_sample_rate=0.1,
    )


def _yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_renderer_replaces_environment_values_without_editing_sources(tmp_path):
    module = _load_renderer()
    source = REPO_ROOT / "infra/k8s"
    source_before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "rendered-k8s"

    module.render_manifests(source, output, _values(module))

    source_after = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    assert source_after == source_before

    app_overlay = _yaml(output / "overlays/gcp/kustomization.yaml")
    assert app_overlay["images"] == [
        {
            "name": "agentcare-backend",
            "newName": (
                "europe-west3-docker.pkg.dev/"
                "agentcare-prod-123/agentcare/backend"
            ),
            "newTag": "0123456789abcdef0123456789abcdef01234567",
        },
        {
            "name": "agentcare-frontend",
            "newName": (
                "europe-west3-docker.pkg.dev/"
                "agentcare-prod-123/agentcare/frontend"
            ),
            "newTag": "0123456789abcdef0123456789abcdef01234567",
        },
    ]

    runtime = _yaml(output / "overlays/gcp/configmap-runtime.yaml")["data"]
    assert runtime == {
        "APP_RELEASE": "0123456789abcdef0123456789abcdef01234567",
        "FRONTEND_ORIGIN": "https://agentcare.example.com",
        "GOOGLE_CLOUD_LOCATION": "europe-west3",
        "GOOGLE_CLOUD_PROJECT": "agentcare-prod-123",
        "LANGFUSE_BASE_URL": "https://cloud.langfuse.com",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-example",
        "LANGFUSE_SAMPLE_RATE": "0.1",
        "LLM_PROFILE": "vertex",
        "INJECTION_GUARD_MODEL": "",
        "SCHEDULER_ENABLED": "false",
    }

    rendered_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in output.rglob("*.yaml")
    )
    assert "_PLACEHOLDER" not in rendered_text


def test_renderer_rejects_mutable_or_malformed_release_tags():
    module = _load_renderer()

    with pytest.raises(ValueError, match="40-character lowercase Git commit SHA"):
        module.DeploymentValues(
            project_id="agentcare-prod-123",
            region="europe-west3",
            image_tag="latest",
            documents_bucket="agentcare-prod-123-agentcare-documents",
            model_armor_template=(
                "projects/agentcare-prod-123/locations/europe-west3/"
                "templates/agentcare-guard"
            ),
            public_url="https://agentcare.example.com",
            llm_profile="vertex",
            langfuse_public_key="",
            langfuse_base_url="https://cloud.langfuse.com",
            langfuse_sample_rate=0.1,
        )


def test_renderer_rejects_missing_required_environment_value():
    module = _load_renderer()
    environment = {
        "GCP_PROJECT_ID": "agentcare-prod-123",
        "GCP_REGION": "europe-west3",
    }

    with pytest.raises(ValueError, match="missing deployment environment variables"):
        module.DeploymentValues.from_environment(environment)


def test_renderer_uses_safe_langfuse_defaults_for_blank_optional_variables():
    module = _load_renderer()
    environment = {
        "GCP_PROJECT_ID": "agentcare-prod-123",
        "GCP_REGION": "europe-west3",
        "IMAGE_TAG": "0123456789abcdef0123456789abcdef01234567",
        "DOCUMENTS_BUCKET": "agentcare-prod-123-agentcare-documents",
        "MODEL_ARMOR_TEMPLATE": (
            "projects/agentcare-prod-123/locations/europe-west3/"
            "templates/agentcare-guard"
        ),
        "PUBLIC_URL": "https://agentcare.example.com",
        "LLM_PROFILE": "vertex",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_BASE_URL": "",
        "LANGFUSE_SAMPLE_RATE": "",
    }

    values = module.DeploymentValues.from_environment(environment)

    assert values.langfuse_public_key == ""
    assert values.langfuse_base_url == "https://cloud.langfuse.com"
    assert values.langfuse_sample_rate == 0
