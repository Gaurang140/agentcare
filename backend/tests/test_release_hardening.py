"""Release invariants that prevent a green CI run from shipping a weak runtime."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _yaml(path: str) -> dict:
    value = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _yaml_documents(path: str) -> list[dict]:
    return [
        value
        for value in yaml.safe_load_all(
            (ROOT / path).read_text(encoding="utf-8")
        )
        if isinstance(value, dict)
    ]


def test_gcp_runtime_uses_vertex_and_external_scheduler():
    runtime = _yaml("infra/k8s/overlays/gcp/configmap-runtime.yaml")["data"]
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert runtime["LLM_PROFILE"] == "LLM_PROFILE_PLACEHOLDER"
    assert "LLM_PROFILE ?= vertex" in makefile
    assert runtime["INJECTION_GUARD_MODEL"] == ""
    assert runtime["SCHEDULER_ENABLED"] == "false"


def test_gcp_release_has_redundant_rolling_workloads_and_disruption_budgets():
    patches = _yaml_documents(
        "infra/k8s/overlays/gcp/availability.yaml"
    )
    by_name = {item["metadata"]["name"]: item for item in patches}

    for name in ("backend", "frontend"):
        deployment = by_name[name]
        assert deployment["spec"]["replicas"] == 2
        assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
        assert deployment["spec"]["strategy"]["rollingUpdate"] == {
            "maxUnavailable": 0,
            "maxSurge": 1,
        }
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["startupProbe"]["httpGet"]["path"]
        assert container["lifecycle"]["preStop"]["exec"]["command"]

    budgets = _yaml_documents("infra/k8s/overlays/gcp/poddisruptionbudgets.yaml")
    assert {item["metadata"]["name"] for item in budgets} == {
        "backend-availability",
        "frontend-availability",
    }
    assert all(item["spec"]["minAvailable"] == 1 for item in budgets)


def test_gcp_cronjobs_replace_the_in_process_scheduler():
    jobs = _yaml_documents("infra/k8s/overlays/gcp/cronjobs.yaml")

    assert {job["metadata"]["name"] for job in jobs} == {
        "send-due-reminders",
        "recover-stalled-workflows",
    }
    assert all(job["spec"]["concurrencyPolicy"] == "Forbid" for job in jobs)
    commands = [
        job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
            "args"
        ]
        for job in jobs
    ]
    assert ["reminders"] in commands
    assert ["recovery"] in commands


def test_migration_job_is_explicit_and_provisions_private_staff():
    job = _yaml("infra/k8s/overlays/gcp-migration/migration-job.yaml")
    container = job["spec"]["template"]["spec"]["containers"][0]
    command = " ".join(container["command"] + container["args"])

    assert "alembic upgrade head" in command
    assert "python -m app.db.seed" in command
    assert "python -m app.db.provision_staff" in command


def test_ci_migrates_postgresql_17_and_rolls_back_a_failed_release():
    workflow = _yaml(".github/workflows/ci.yml")
    test_service = workflow["jobs"]["test"]["services"]["postgres"]
    migration = workflow["jobs"]["migrations"]

    assert test_service["image"] == "postgres:17"
    assert migration["services"]["postgres"]["image"] == "postgres:17"
    assert migration["env"]["DATABASE_URL"].startswith(
        "postgresql+psycopg://"
    )

    deploy = workflow["jobs"]["deploy-production"]["steps"]
    capture = next(
        step
        for step in deploy
        if step.get("name") == "capture current runtime for rollback"
    )
    rollback = next(
        step
        for step in deploy
        if step.get("name") == "roll back failed rollout"
    )
    assert "failure()" in rollback["if"]
    assert 'capture_resource deployment "$app" "deployment-$app"' in capture["run"]
    assert "rollout undo" not in rollback["run"]
    assert 'restore_resource deployment "$app" "deployment-$app"' in rollback["run"]


def test_terraform_enables_vertex_and_cloud_sql_recovery_by_default():
    variables = (ROOT / "infra/terraform/variables.tf").read_text(
        encoding="utf-8"
    )
    cloud_sql = (ROOT / "infra/terraform/modules/cloud-sql/main.tf").read_text(
        encoding="utf-8"
    )
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    vertex_block = variables.split('variable "enable_vertex_ai"', 1)[1].split(
        "}", 1
    )[0]
    assert "default     = true" in vertex_block
    assert "ENABLE_VERTEX_AI ?= true" in makefile
    assert "LLM_PROFILE ?= vertex" in makefile
    assert "backup_configuration" in cloud_sql
    assert "point_in_time_recovery_enabled = true" in cloud_sql
    assert "maintenance_window" in cloud_sql
