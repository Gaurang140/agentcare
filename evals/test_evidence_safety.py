import json

from evals import phase1_run, phase2_score


def _enriched(run_id: str) -> dict:
    return {
        "run": {"run_id": run_id, "base_url": "http://localhost:8000"},
        "admin_samples": [],
        "guardrail_samples": [],
    }


def test_phase1_refuses_to_overwrite_preserved_baseline(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "enriched-nokey-baseline.json"
    baseline.write_text("preserved", encoding="utf-8")
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps({"admin_samples": [], "guardrail_samples": []}),
        encoding="utf-8",
    )
    calls = 0

    def fake_run_phase1(*_args):
        nonlocal calls
        calls += 1
        return _enriched("pending")

    monkeypatch.setattr(phase1_run, "run_phase1", fake_run_phase1)

    result = phase1_run.main(
        [
            "--dataset",
            str(dataset),
            "--out-dir",
            str(tmp_path),
            "--run-id",
            "nokey-baseline",
        ]
    )

    assert result == 2
    assert calls == 0
    assert baseline.read_text(encoding="utf-8") == "preserved"
    assert "preserved eval evidence" in capsys.readouterr().err


def test_phase2_refuses_to_overwrite_preserved_baseline(tmp_path, capsys):
    input_path = tmp_path / "enriched-nokey-baseline.json"
    input_path.write_text(json.dumps(_enriched("nokey-baseline")), encoding="utf-8")
    scores_path = tmp_path / "scores-nokey-baseline.json"
    summary_path = tmp_path / "summary-nokey-baseline.md"
    scores_path.write_text("preserved scores", encoding="utf-8")
    summary_path.write_text("preserved summary", encoding="utf-8")

    result = phase2_score.main(
        [
            "--input",
            str(input_path),
            "--results-dir",
            str(tmp_path),
            "--no-judge",
        ]
    )

    assert result == 2
    assert scores_path.read_text(encoding="utf-8") == "preserved scores"
    assert summary_path.read_text(encoding="utf-8") == "preserved summary"
    assert "preserved eval evidence" in capsys.readouterr().err


def test_degraded_summary_reproduction_is_no_key_and_uses_scratch_output():
    enriched = {
        "run": {
            "run_id": "nokey-baseline",
            "base_url": "http://localhost:8000",
            "started_at": "now",
        },
        "admin_samples": [
            {
                "id": "admin-1",
                "domain": "book",
                "language": "en",
                "request": "Book an appointment.",
                "expected_intent": "book",
                "expected_department": "Cardiology",
                "expected_steps": ["coordinator"],
                "actual_intent": None,
                "actual_department": None,
                "actual_steps": ["coordinator"],
                "actual_status": "waiting_approval",
                "actual_response": None,
                "actual_escalation": {"severity": "agent_failure"},
                "actual_error": "Missing credentials.",
            }
        ],
        "guardrail_samples": [],
    }
    summary = phase2_score.render_summary(phase2_score.score(enriched, judge=False))

    assert "no working model credential or provider access" in summary
    assert "--run-id scratch-nokey-baseline" in summary
    assert "--run-id nokey-baseline\n" not in summary
    for assignment in (
        "LLM_PROFILE=groq",
        "LLM_API_KEY=",
        "LLM_FALLBACK_API_KEY=",
        "LLM_FALLBACK_BASE_URL=",
        "LLM_FALLBACK_MODEL=",
        "MODEL_ARMOR_TEMPLATE=",
        "JUDGE_GROQ=",
        "JUDGE_MODEL=",
        "JUDGE_BASE_URL=",
    ):
        assert assignment in summary
