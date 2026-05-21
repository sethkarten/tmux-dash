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
