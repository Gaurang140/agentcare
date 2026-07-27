"""Regression tests for the committed Compose and Kubernetes deployment config."""

from __future__ import annotations

import re
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
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
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


def test_kubernetes_probes_separate_process_liveness_from_database_readiness():
    documents = list(
        yaml.safe_load_all(
            (REPO_ROOT / "infra/k8s/base/backend.yaml").read_text(encoding="utf-8")
        )
    )
    deployment = _resource(documents, "Deployment", "backend")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["livenessProbe"]["httpGet"]["path"] == "/api/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/health"


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


def test_gcp_ingress_redirects_http_to_https():
    overlay = _load_yaml(REPO_ROOT / "infra/k8s/overlays/gcp/kustomization.yaml")
    assert "frontendconfig.yaml" in overlay["resources"]

    frontend_config = _load_yaml(
        REPO_ROOT / "infra/k8s/overlays/gcp/frontendconfig.yaml"
    )
    assert frontend_config["kind"] == "FrontendConfig"
    assert frontend_config["spec"]["redirectToHttps"] == {
        "enabled": True,
        "responseCodeName": "PERMANENT_REDIRECT",
    }
    assert frontend_config["spec"]["sslPolicy"] == "agentcare-modern-tls"

    terraform = (REPO_ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")
    assert 'resource "google_compute_ssl_policy" "frontend"' in terraform
    assert 'profile         = "MODERN"' in terraform
    assert 'min_tls_version = "TLS_1_2"' in terraform

    ingress_documents = list(
        yaml.safe_load_all(
            (REPO_ROOT / "infra/k8s/overlays/gcp/ingress.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    ingress = _resource(ingress_documents, "Ingress", "agentcare")
    assert ingress["metadata"]["annotations"][
        "networking.gke.io/v1beta1.FrontendConfig"
    ] == (
        "agentcare-frontendconfig"
    )
    assert "networking.gke.io/frontend-config" not in ingress["metadata"]["annotations"]


def test_gcp_database_requires_encrypted_transport():
    cloud_sql = (
        REPO_ROOT / "infra/terraform/modules/cloud-sql/main.tf"
    ).read_text(encoding="utf-8")
    assert re.search(r'ssl_mode\s*=\s*"ENCRYPTED_ONLY"', cloud_sql)

    secret_template = _load_yaml(REPO_ROOT / "infra/k8s/base/secret.example.yaml")
    assert secret_template["stringData"]["DATABASE_URL"].endswith("?sslmode=require")


def test_gcs_runtime_identity_can_create_but_not_overwrite_objects():
    iam = (REPO_ROOT / "infra/terraform/modules/iam/main.tf").read_text(
        encoding="utf-8"
    )
    assert 'role   = "roles/storage.objectCreator"' in iam
    assert "roles/storage.objectAdmin" not in iam


def test_model_armor_has_regional_psc_endpoint_and_private_dns():
    module = (
        REPO_ROOT / "infra/terraform/modules/model-armor/main.tf"
    ).read_text(encoding="utf-8")

    assert 'purpose      = "GCE_ENDPOINT"' in module
    assert 'target_google_api = "modelarmor.${var.location}.rep.googleapis.com"' in module
    assert "target_google_api = local.target_google_api" in module
    assert 'access_type       = "REGIONAL"' in module
    assert re.search(r'visibility\s*=\s*"private"', module)
    assert re.search(r'type\s*=\s*"A"', module)
    assert "google_compute_address.endpoint[0].address" in module


def test_model_armor_location_is_an_operator_substitution_not_a_second_default():
    model_armor_config = _load_yaml(
        REPO_ROOT / "infra/k8s/overlays/gcp/configmap-model-armor.yaml"
    )
    assert model_armor_config["data"]["MODEL_ARMOR_LOCATION"] == "REGION"


def test_gke_uses_a_dedicated_node_identity_with_explicit_pull_permissions():
    iam = (REPO_ROOT / "infra/terraform/modules/iam/main.tf").read_text(
        encoding="utf-8"
    )
    gke = (REPO_ROOT / "infra/terraform/modules/gke-autopilot/main.tf").read_text(
        encoding="utf-8"
    )
    root = (REPO_ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")

    assert 'resource "google_service_account" "gke_nodes"' in iam
    assert '"roles/container.defaultNodeServiceAccount"' in iam
    assert 'resource "google_artifact_registry_repository_iam_member"' in iam
    assert '"roles/artifactregistry.reader"' in iam
    assert "service_account = var.node_service_account_email" in gke
    assert "node_service_account_email = module.iam.gke_node_service_account_email" in root
    assert "depends_on = [module.iam]" in root


def test_gcp_overlay_declares_managed_prometheus_collection():
    overlay = _load_yaml(REPO_ROOT / "infra/k8s/overlays/gcp/kustomization.yaml")
    assert "podmonitoring.yaml" in overlay["resources"]

    monitor = _load_yaml(REPO_ROOT / "infra/k8s/overlays/gcp/podmonitoring.yaml")
    assert monitor["apiVersion"] == "monitoring.googleapis.com/v1"
    assert monitor["kind"] == "PodMonitoring"
    assert monitor["spec"]["selector"]["matchLabels"] == {"app": "backend"}
    assert monitor["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "30s"}
    ]


def test_gcp_runbook_checks_required_plugins_and_exercises_gcs_upload_path():
    runbook = (REPO_ROOT / "docs/deployment-gcp.md").read_text(encoding="utf-8")

    assert "gke-gcloud-auth-plugin --version" in runbook
    assert "docker buildx version" in runbook
    assert "openssl version" in runbook
    assert "monitoring.googleapis.com" in runbook
    assert "logging.googleapis.com" in runbook
    assert "serviceusage.googleapis.com" in runbook
    assert "cloudresourcemanager.googleapis.com" in runbook
    assert 'curl -fsS --max-time 10 "https://YOUR_DOMAIN/api/health"' in runbook
    assert 'curl -fsSI --max-time 10 "https://YOUR_DOMAIN/api/health"' not in runbook
    assert "files=@/tmp/${SMOKE_FILENAME};type=text/plain" in runbook
    assert 'gcloud storage ls "gs://${DOCUMENTS_BUCKET}/**${SMOKE_FILENAME}"' in runbook
    assert "openssl rand -hex 32" in runbook
    assert "JWT_SECRET=.{32,}" in runbook


def test_ci_defaults_to_read_only_and_verifies_kubeconform_archive():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    assert workflow["permissions"] == {"contents": "read"}

    install = next(
        step["run"]
        for step in workflow["jobs"]["manifests"]["steps"]
        if step.get("name") == "install kubeconform"
    )
    assert "sha256sum -c" in install
    assert "| tar " not in install
    assert "v0.8.0" in install
    assert "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883" in install
    assert "v0.7.0" not in install


def test_ci_validates_terraform_with_pinned_hashicorp_setup():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    job = workflow["jobs"]["infrastructure"]
    assert job["defaults"]["run"]["working-directory"] == "infra/terraform"

    setup = next(
        step
        for step in job["steps"]
        if step.get("name") == "set up Terraform"
    )
    assert setup["uses"] == (
        "hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e"
    )
    assert setup["with"] == {
        "terraform_version": "1.15.8",
        "terraform_wrapper": False,
    }

    commands = "\n".join(
        step["run"] for step in job["steps"] if isinstance(step.get("run"), str)
    )
    assert "terraform fmt -check -recursive" in commands
    assert "terraform init -backend=false -input=false" in commands
    assert "terraform validate -no-color" in commands


def test_ci_pins_first_party_actions_to_full_release_commits():
    expected = {
        "actions/checkout": (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        ),
        "actions/setup-python": (
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
        ),
        "actions/setup-node": (
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
        ),
    }
    found: set[str] = set()

    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        workflow = _load_yaml(path)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses", "").split("@", maxsplit=1)[0]
                if action in expected:
                    found.add(action)
                    assert step["uses"] == expected[action]

    assert found == set(expected)


def test_frontend_image_does_not_copy_an_untracked_public_directory():
    dockerfile = (REPO_ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")

    assert "/app/public" not in dockerfile


def test_ci_gates_eval_evidence_and_production_frontend_advisories():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    backend_steps = workflow["jobs"]["test"]["steps"]
    frontend_steps = workflow["jobs"]["frontend"]["steps"]

    backend_commands = "\n".join(
        step["run"] for step in backend_steps if isinstance(step.get("run"), str)
    )
    frontend_commands = "\n".join(
        step["run"] for step in frontend_steps if isinstance(step.get("run"), str)
    )

    assert "ruff check backend evals" in backend_commands
    assert "pytest -q backend evals/test_evidence_safety.py" in backend_commands
    assert "evals/phase2_score.py --selftest" in backend_commands
    assert "npm audit --omit=dev --audit-level=high" in frontend_commands


def test_frontend_pins_patched_next_runtime_transitives():
    package = _load_yaml(REPO_ROOT / "frontend/package.json")

    assert "shadcn" not in package["dependencies"]
    assert package["devDependencies"]["shadcn"]
    assert package["overrides"] == {
        "postcss": "8.5.22",
        "sharp": "0.35.3",
    }
