"""Regression tests for the committed Compose and Kubernetes deployment config."""

from __future__ import annotations

import re
import subprocess
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


def _make_dry_run(target: str) -> str:
    result = subprocess.run(
        [
            "make",
            "-n",
            target,
            "PROJECT_ID=agentcare-example",
            "REGION=europe-west3",
            "GCS_LOCATION=europe-west3",
            "NETWORK_NAME=default",
            "SUBNETWORK_NAME=default",
            "ENABLE_CLOUD_SQL=true",
            "ENABLE_MODEL_ARMOR=true",
            "ENABLE_VERTEX_AI=false",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


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


def test_backend_docker_context_is_an_explicit_allowlist():
    patterns = (
        REPO_ROOT / ".dockerignore"
    ).read_text(encoding="utf-8").splitlines()

    assert patterns[0] == "*"
    backend_tree_index = patterns.index("!backend/**")
    assert patterns.index("!requirements.txt") < backend_tree_index
    assert patterns.index("!backend/") < backend_tree_index

    forbidden_after_allow = {
        "**/.env*",
        "**/.git",
        "**/.terraform",
        "**/*.tfstate*",
        "**/__pycache__",
        "**/*.py[cod]",
        "**/*.db",
        "**/uploads",
        "**/.pytest_cache",
        "**/.ruff_cache",
    }
    security_rules = patterns[backend_tree_index + 1 :]
    assert forbidden_after_allow.issubset(set(security_rules))
    assert not any(pattern.startswith("!") for pattern in security_rules)


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
            "newName": (
                "REGION_PLACEHOLDER-docker.pkg.dev/"
                "PROJECT_ID_PLACEHOLDER/agentcare/backend"
            ),
            "newTag": "IMAGE_TAG_PLACEHOLDER",
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
    assert re.search(
        r'resource "google_network_connectivity_regional_endpoint" "this"'
        r".*?address\s*=\s*google_compute_address\.endpoint\[0\]\.id",
        module,
        re.DOTALL,
    )
    assert re.search(
        r'resource "google_dns_record_set" "this"'
        r".*?rrdatas\s*=\s*\[google_compute_address\.endpoint\[0\]\.address\]",
        module,
        re.DOTALL,
    )


def test_model_armor_location_is_an_operator_substitution_not_a_second_default():
    model_armor_config = _load_yaml(
        REPO_ROOT / "infra/k8s/overlays/gcp/configmap-model-armor.yaml"
    )
    assert (
        model_armor_config["data"]["MODEL_ARMOR_LOCATION"]
        == "REGION_PLACEHOLDER"
    )


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


def test_gke_private_nodes_have_scoped_cloud_nat_egress():
    gke = (REPO_ROOT / "infra/terraform/modules/gke-autopilot/main.tf").read_text(
        encoding="utf-8"
    )

    assert re.search(
        r"private_cluster_config\s*{"
        r".*?enable_private_nodes\s*=\s*true"
        r".*?enable_private_endpoint\s*=\s*false"
        r".*?}",
        gke,
        re.DOTALL,
    )
    assert 'resource "google_compute_router" "egress"' in gke
    assert 'resource "google_compute_router_nat" "egress"' in gke
    assert 'source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"' in gke
    assert re.search(
        r"subnetwork\s*{"
        r".*?name\s*=\s*data\.google_compute_subnetwork\.cluster\.id"
        r'.*?source_ip_ranges_to_nat\s*=\s*\["ALL_IP_RANGES"\]'
        r".*?}",
        gke,
        re.DOTALL,
    )
    assert re.search(
        r"log_config\s*{"
        r".*?enable\s*=\s*true"
        r'.*?filter\s*=\s*"ERRORS_ONLY"'
        r".*?}",
        gke,
        re.DOTALL,
    )
    assert "depends_on = [google_compute_router_nat.egress]" in gke


def test_workload_identity_binding_uses_the_created_gke_pool():
    iam = (REPO_ROOT / "infra/terraform/modules/iam/main.tf").read_text(
        encoding="utf-8"
    )
    iam_outputs = (
        REPO_ROOT / "infra/terraform/modules/iam/outputs.tf"
    ).read_text(encoding="utf-8")
    gke_outputs = (
        REPO_ROOT / "infra/terraform/modules/gke-autopilot/outputs.tf"
    ).read_text(encoding="utf-8")
    root = (REPO_ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")

    binding = (
        'resource "google_service_account_iam_member" '
        '"backend_workload_identity_user"'
    )
    assert binding not in iam
    assert binding in root
    assert 'output "backend_service_account_name"' in iam_outputs
    assert 'output "workload_pool"' in gke_outputs
    assert (
        "google_container_cluster.this.workload_identity_config[0].workload_pool"
        in gke_outputs
    )
    assert re.search(
        r"service_account_id\s*=\s*module\.iam\.backend_service_account_name",
        root,
    )
    assert re.search(
        r'member\s*=\s*"serviceAccount:\${module\.gke\.workload_pool}'
        r'\[default/agentcare-backend\]"',
        root,
    )
    assert re.search(
        r"moved\s*{"
        r"\s*from\s*=\s*module\.iam\.google_service_account_iam_member"
        r"\.backend_workload_identity_user"
        r"\s*to\s*=\s*google_service_account_iam_member"
        r"\.backend_workload_identity_user"
        r"\s*}",
        root,
    )


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


def test_kubernetes_workload_sources_never_default_to_latest():
    manifests = (
        REPO_ROOT / "infra/k8s/base/backend.yaml",
        REPO_ROOT / "infra/k8s/base/frontend.yaml",
        REPO_ROOT / "infra/k8s/overlays/gcp-migration/migration-job.yaml",
    )

    for manifest in manifests:
        assert ":latest" not in manifest.read_text(encoding="utf-8")


def test_terraform_reserves_the_ingress_ip_used_by_the_gcp_overlay():
    root = (REPO_ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")
    outputs = (REPO_ROOT / "infra/terraform/outputs.tf").read_text(
        encoding="utf-8"
    )
    ingress_documents = list(
        yaml.safe_load_all(
            (
                REPO_ROOT / "infra/k8s/overlays/gcp/ingress.yaml"
            ).read_text(encoding="utf-8")
        )
    )
    ingress = ingress_documents[0]

    assert 'resource "google_compute_global_address" "ingress"' in root
    assert 'name    = "agentcare-ingress"' in root
    assert 'output "ingress_ip_address"' in outputs
    assert (
        ingress["metadata"]["annotations"][
            "kubernetes.io/ingress.global-static-ip-name"
        ]
        == "agentcare-ingress"
    )


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


def test_ci_validates_the_bootstrap_and_main_terraform_stacks():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    steps = workflow["jobs"]["infrastructure"]["steps"]

    bootstrap_steps = [
        step
        for step in steps
        if step.get("working-directory") == "infra/bootstrap"
    ]
    bootstrap_commands = "\n".join(
        step["run"]
        for step in bootstrap_steps
        if isinstance(step.get("run"), str)
    )

    assert "terraform fmt -check" in bootstrap_commands
    assert "terraform init -backend=false -input=false" in bootstrap_commands
    assert "terraform validate -no-color" in bootstrap_commands


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


def test_ci_deploys_main_only_after_every_release_gate():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    deploy = workflow["jobs"].get("deploy-production")

    assert deploy is not None, "CI has no production deployment job"
    assert deploy["needs"] == [
        "test",
        "frontend",
        "migrations",
        "infrastructure",
        "manifests",
        "secret-scan",
    ]
    assert deploy["if"] == (
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
        " && github.ref == 'refs/heads/main'"
        " && vars.DEPLOY_ENABLED == 'true'"
    )
    assert deploy["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert deploy["environment"] == {
        "name": "production",
        "url": "${{ vars.PUBLIC_URL }}",
    }
    assert deploy["concurrency"] == {
        "group": "agentcare-production",
        "cancel-in-progress": False,
    }


def test_ci_release_uses_keyless_gcp_auth_and_pinned_deployment_actions():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    steps = workflow["jobs"]["deploy-production"]["steps"]
    used_actions = {
        step["uses"].split("@", maxsplit=1)[0]: step["uses"]
        for step in steps
        if "uses" in step
    }

    assert used_actions["google-github-actions/auth"] == (
        "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093"
    )
    assert used_actions["google-github-actions/setup-gcloud"] == (
        "google-github-actions/setup-gcloud@aa5489c8933f4cc7a4f7d45035b3b1440c9c10db"
    )
    assert used_actions["google-github-actions/get-gke-credentials"] == (
        "google-github-actions/get-gke-credentials@"
        "3da1e46a907576cefaa90c484278bb5b259dd395"
    )
    assert used_actions["docker/setup-buildx-action"] == (
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
    )
    assert used_actions["docker/build-push-action"] == (
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
    )

    auth = next(
        step
        for step in steps
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert auth["with"] == {
        "project_id": "${{ vars.GCP_PROJECT_ID }}",
        "workload_identity_provider": (
            "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}"
        ),
        "service_account": "${{ vars.GCP_DEPLOYER_SERVICE_ACCOUNT }}",
    }
    assert "credentials_json" not in auth["with"]


def test_ci_release_migrates_before_rollout_and_smoke_tests_public_health():
    workflow = _load_yaml(REPO_ROOT / ".github/workflows/ci.yml")
    deploy = workflow["jobs"]["deploy-production"]
    steps = deploy["steps"]
    commands = "\n".join(
        step["run"] for step in steps if isinstance(step.get("run"), str)
    )
    step_names = [step.get("name") for step in steps]

    assert step_names.index("run database migration") < step_names.index(
        "deploy application"
    )
    assert "kubectl wait --for=condition=complete job/backend-migrate" in commands
    assert "kubectl rollout status deployment/backend" in commands
    assert "kubectl rollout status deployment/frontend" in commands
    assert '"${PUBLIC_URL}/api/health"' in commands
    assert "terraform apply" not in commands
    assert "terraform destroy" not in commands

    build_steps = [
        step for step in steps if step.get("uses", "").startswith(
            "docker/build-push-action@"
        )
    ]
    assert len(build_steps) == 2
    for step in build_steps:
        assert step["with"]["push"] is True
        assert "${{ github.sha }}" in step["with"]["tags"]
        assert ":latest" not in step["with"]["tags"]


def test_frontend_pins_patched_next_runtime_transitives():
    package = _load_yaml(REPO_ROOT / "frontend/package.json")

    assert "shadcn" not in package["dependencies"]
    assert package["devDependencies"]["shadcn"]
    assert package["overrides"] == {
        "postcss": "8.5.22",
        "sharp": "0.35.3",
    }


def test_terraform_bootstrap_protects_remote_state_and_uses_no_key_file():
    bootstrap = REPO_ROOT / "infra/bootstrap/main.tf"
    assert bootstrap.is_file(), "the keyless CI/CD bootstrap stack is missing"
    source = bootstrap.read_text(encoding="utf-8")

    assert 'resource "google_storage_bucket" "terraform_state"' in source
    assert "uniform_bucket_level_access = true" in source
    assert 'public_access_prevention    = "enforced"' in source
    assert re.search(
        r"versioning\s*{\s*enabled\s*=\s*true\s*}",
        source,
        re.DOTALL,
    )
    assert "force_destroy               = false" in source
    assert "google_service_account_key" not in source

    backend = (REPO_ROOT / "infra/terraform/backend.tf").read_text(
        encoding="utf-8"
    )
    assert 'backend "gcs"' in backend
    assert 'prefix = "agentcare/production"' in backend
    assert 'backend "local"' not in backend
    assert "bucket =" not in backend


def test_terraform_bootstrap_enables_every_service_used_by_the_gcp_stack():
    source = (
        REPO_ROOT / "infra/bootstrap/main.tf"
    ).read_text(encoding="utf-8")

    for service in (
        "aiplatform.googleapis.com",
        "artifactregistry.googleapis.com",
        "cloudresourcemanager.googleapis.com",
        "compute.googleapis.com",
        "container.googleapis.com",
        "dns.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "logging.googleapis.com",
        "modelarmor.googleapis.com",
        "monitoring.googleapis.com",
        "networkconnectivity.googleapis.com",
        "servicenetworking.googleapis.com",
        "serviceusage.googleapis.com",
        "sqladmin.googleapis.com",
        "storage.googleapis.com",
        "sts.googleapis.com",
    ):
        assert f'"{service}"' in source


def test_terraform_bootstrap_restricts_github_identity_to_repo_id_and_main():
    source = (
        REPO_ROOT / "infra/bootstrap/main.tf"
    ).read_text(encoding="utf-8")

    assert (
        'resource "google_iam_workload_identity_pool_provider" "github"'
        in source
    )
    assert '"attribute.repository_id"       = "assertion.repository_id"' in source
    assert (
        '"attribute.repository_owner_id" = "assertion.repository_owner_id"'
        in source
    )
    assert '"attribute.ref"                 = "assertion.ref"' in source
    assert re.search(
        r'"attribute\.environment"\s*=\s*"assertion\.environment"',
        source,
    )
    assert re.search(
        r'"attribute\.workflow_ref"\s*=\s*"assertion\.workflow_ref"',
        source,
    )
    assert "assertion.repository_id == '${var.github_repository_id}'" in source
    assert (
        "assertion.repository_owner_id == "
        "'${var.github_repository_owner_id}'"
        in source
    )
    assert "assertion.ref == 'refs/heads/${var.deploy_branch}'" in source
    assert "assertion.environment == '${var.deploy_environment}'" in source
    assert (
        "assertion.workflow_ref.endsWith("
        "'/.github/workflows/${var.deploy_workflow_file}"
        "@refs/heads/${var.deploy_branch}')"
        in source
    )
    assert "attribute.repository_id/${var.github_repository_id}" in source


def test_terraform_bootstrap_keeps_github_deployer_least_privilege():
    source = (
        REPO_ROOT / "infra/bootstrap/main.tf"
    ).read_text(encoding="utf-8")

    def _role_block(name: str) -> str:
        match = re.search(rf"{name}\s*=\s*toset\(\[(.*?)\]\)", source, re.DOTALL)
        assert match is not None, f"{name} is not declared as a toset list"
        return match.group(1)

    deployment_roles = _role_block("deployment_roles")

    expected_roles = {
        "roles/artifactregistry.writer",
        "roles/container.clusterViewer",
        "roles/container.developer",
        "roles/serviceusage.serviceUsageConsumer",
    }
    assert set(re.findall(r'"(roles/[^"]+)"', deployment_roles)) == expected_roles
    assert "infrastructure_roles" not in source
    assert "github_infra" not in source
    assert "infra_service_account_email" not in (
        REPO_ROOT / "infra/bootstrap/outputs.tf"
    ).read_text(encoding="utf-8")

    for excessive_role in (
        '"roles/owner"',
        '"roles/editor"',
        '"roles/resourcemanager.projectIamAdmin"',
        '"roles/iam.serviceAccountAdmin"',
    ):
        assert excessive_role not in source


def test_github_workflows_cannot_apply_or_destroy_terraform():
    assert not (REPO_ROOT / ".github/workflows/infrastructure.yml").exists()

    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        workflow = _load_yaml(path)
        commands = "\n".join(
            step["run"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if isinstance(step.get("run"), str)
        )
        assert not re.search(
            r"(?m)^\s*terraform\b[^\n]*\b(?:apply|destroy)\b",
            commands,
        ), f"{path.name} can mutate Terraform-managed infrastructure"


def test_make_uses_one_complete_terraform_input_set_for_up_and_down():
    expected = {
        '-var="project_id=agentcare-example"',
        '-var="region=europe-west3"',
        '-var="gcs_location=europe-west3"',
        '-var="network_name=default"',
        '-var="subnetwork_name=default"',
        '-var="enable_cloud_sql=true"',
        '-var="enable_model_armor=true"',
        '-var="enable_vertex_ai=false"',
    }

    for target in ("gcp-up", "gcp-down", "gcp-cleanup"):
        output = _make_dry_run(target)
        assert expected.issubset(set(re.findall(r'-var="[^"]+"', output)))


def test_make_targets_the_exact_terraform_cluster_and_bucket():
    down = _make_dry_run("gcp-down")
    status = _make_dry_run("gcp-status")

    for output in (down, status):
        assert "output -raw gke_cluster_name" in output
        assert "output -raw gke_cluster_location" in output
        assert "gcloud container clusters get-credentials" in output
        assert "kubectl --context" in output

    assert "output -raw documents_bucket_name" in down
    assert "gs://agentcare-example-agentcare-documents" not in down
    assert not re.search(r"(?m)^kubectl delete\b", down)


def test_public_docs_do_not_claim_the_destroyed_deployment_is_live():
    public_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md")))
    )

    assert "agentcare.136-69-65-187.sslip.io" not in public_docs
    assert "application is live" not in public_docs.lower()
