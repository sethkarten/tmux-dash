from tmux_dash import app


def test_agent_pane_target_keeps_explicit_tmux_targets() -> None:
    assert app.agent_pane_target("orch:0.0") == "orch:0.0"
    assert app.agent_pane_target("%233") == "%233"


def test_agent_pane_target_prefers_main_codex_pane(monkeypatch) -> None:
    monkeypatch.setattr(app, "_swapped_session", lambda: None)
    monkeypatch.setattr(app, "_pane_exists", lambda target: target == "programBench:0.0")

    assert app.agent_pane_target("programBench") == "programBench:0.0"


def test_agent_pane_target_uses_orchestrator_view_when_swapped(monkeypatch) -> None:
    monkeypatch.setattr(app, "ORCH_TARGET", "orch:0.0")
    monkeypatch.setattr(app, "_swapped_session", lambda: "programBench")

    assert app.agent_pane_target("programBench") == "orch:0.0"


def test_guard_active_summary_replaces_idle_claim() -> None:
    raw = "Counts: 38 rows, 0 errors\n• Working (1h 52m) · 2 background jobs"
    summary = "The agent is not showing an active command or background job; it is at a zsh prompt."

    guarded = app._guard_active_summary(raw, summary)

    assert guarded.startswith("Active:")
    assert "2 background jobs" in guarded


def test_fallback_summary_uses_active_marker() -> None:
    raw = "Counts: 38 rows, 0 errors\n• Working (1h 52m) · 2 background jobs"

    summary = app._fallback_summary(raw)

    assert summary.startswith("Active:")
    assert "2 background jobs" in summary


def test_fallback_summary_ignores_placeholders() -> None:
    assert app._fallback_summary("·\n…") == "No recent pane output."


def test_summarize_sync_uses_low_reasoning_key(monkeypatch) -> None:
    app._cache.clear()
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["timeout"] = kwargs["timeout"]
        output_path = args[args.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("Codex summary.")
        return app.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    summary = app._summarize_sync("agent-low-reasoning", "terminal output")

    assert summary == "Codex summary."
    assert "model_reasoning_effort=low" in seen["args"]
    assert seen["timeout"] == app.SUMMARY_TIMEOUT


def test_summarize_sync_skips_codex_for_empty_capture(monkeypatch) -> None:
    app._cache.clear()

    def fake_run(args, **kwargs):
        raise AssertionError("codex should not run for empty captures")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    summary = app._summarize_sync("agent-empty", "·")

    assert summary == "No recent pane output."


def test_summarize_sync_falls_back_on_timeout(monkeypatch) -> None:
    app._cache.clear()

    def fake_run(args, **kwargs):
        raise app.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    summary = app._summarize_sync("agent-timeout", "line one\nline two")

    assert summary == "line one  line two"
