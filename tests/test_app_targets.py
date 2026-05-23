import asyncio

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


def test_subsession_parent_uses_explicit_config(monkeypatch) -> None:
    monkeypatch.setattr(app, "SUBSESSIONS", {"child": "parent"})

    assert app.subsession_parent("child", parents=()) == "parent"


def test_subsession_parent_infers_from_configured_parent(monkeypatch) -> None:
    monkeypatch.setattr(app, "SUBSESSIONS", {})

    parent = app.subsession_parent(
        "emulatorbench_dmg_glm51_rlm_20260522",
        parents=("emulatorBench", "programBench"),
    )

    assert parent == "emulatorBench"


def test_get_sessions_links_auto_subsessions(monkeypatch) -> None:
    monkeypatch.setattr(app, "AGENT_ORDER", ["programBench", "emulatorBench"])
    monkeypatch.setattr(app, "SESSION_LABELS", {})
    monkeypatch.setattr(app, "SUBSESSIONS", {})
    monkeypatch.setattr(app, "EXCLUDE", {"orch"})
    monkeypatch.setattr(app, "PARENT_SESSION_CANDIDATES", ("programBench", "emulatorBench"))

    def fake_run(args, **kwargs):
        return app.subprocess.CompletedProcess(
            args,
            0,
            "orch\nprogramBench\nemulatorBench\nscrapeEnv\nemulatorbench_dmg_rlm\n",
            "",
        )

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app.get_sessions() == [
        "programBench",
        "emulatorBench",
        "scrapeEnv",
        "emulatorbench_dmg_rlm",
    ]


def test_dash_session_for_number_supports_double_digits() -> None:
    dash = app.Dash()
    dash._sessions = [f"s{i}" for i in range(1, 12)]

    assert dash._session_for_number(10) == "s10"
    assert dash._session_for_number(12) is None


def test_orchestrator_pane_prefers_saved_pane_when_swap_flag_missing(monkeypatch) -> None:
    monkeypatch.setattr(app, "ORCH_TARGET", "orch:0.0")
    monkeypatch.setattr(app, "_pane_id", lambda target: "%233" if target == "orch:0.0" else target)
    monkeypatch.setattr(app, "_pane_exists", lambda target: target in {"%233", "%239"})
    monkeypatch.setattr(
        app,
        "_tmux_out",
        lambda args: "%239" if args[-1] == app.ORCH_PANE_OPT else "",
    )

    assert app._orchestrator_pane() == "%239"


def test_restore_orchestrator_recovers_from_missing_swap_flag(monkeypatch) -> None:
    monkeypatch.setattr(app, "ORCH_TARGET", "orch:0.0")
    options = {app.ORCH_PANE_OPT: "%239"}
    current = {"orch:0.0": "%233"}
    calls = []

    def fake_tmux_out(args):
        if args[:2] == ["show-options", "-gqv"]:
            return options.get(args[-1], "")
        return ""

    def fake_pane_id(target):
        return current.get(target, target if target in {"%233", "%239"} else None)

    def fake_tmux(args):
        calls.append(args)
        if args[:2] == ["swap-pane", "-d"]:
            current["orch:0.0"] = "%239"
        return app.subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(app, "_tmux_out", fake_tmux_out)
    monkeypatch.setattr(app, "_pane_id", fake_pane_id)
    monkeypatch.setattr(app, "_pane_exists", lambda target: target in {"%233", "%239"})
    monkeypatch.setattr(app, "_tmux", fake_tmux)
    monkeypatch.setattr(app, "_set_tmux_option", lambda name, value: options.__setitem__(name, value))
    monkeypatch.setattr(app, "_clear_tmux_option", lambda name: options.pop(name, None))
    monkeypatch.setattr(app, "_close_visible_terminal", lambda: None)

    assert app.restore_orchestrator()
    assert ["swap-pane", "-d", "-s", "%239", "-t", "orch:0.0"] in calls
    assert current["orch:0.0"] == "%239"
    assert options[app.ORCH_PANE_OPT] == "%239"
    assert app.SWAPPED_SESSION_OPT not in options


def test_orchestrator_capture_target_uses_saved_pane(monkeypatch) -> None:
    monkeypatch.setattr(app, "_orchestrator_pane", lambda: "%239")

    assert app.orchestrator_capture_target() == "%239"


def test_heartbeat_send_target_restores_before_resolving(monkeypatch) -> None:
    restored = []

    def fake_restore() -> bool:
        restored.append(True)
        return True

    monkeypatch.setattr(app, "ORCH_TARGET", "orch:0.0")
    monkeypatch.setattr(app, "restore_orchestrator", fake_restore)
    monkeypatch.setattr(app, "_pane_id", lambda target: "%239" if target == "orch:0.0" else None)

    target, label = app.heartbeat_send_target()

    assert restored
    assert target == "%239"
    assert label == "orch:0.0 (%239)"


def test_heartbeat_send_target_reports_restore_failure(monkeypatch) -> None:
    monkeypatch.setattr(app, "restore_orchestrator", lambda: False)

    target, label = app.heartbeat_send_target()

    assert target is None
    assert label == "could not restore orchestrator"


def test_heartbeat_sends_to_resolved_orchestrator_pane(monkeypatch) -> None:
    dash = app.Dash()
    sent = {}
    ledger_events = []

    monkeypatch.setattr(dash, "_collect_snapshots", lambda: [])
    monkeypatch.setattr(dash, "_cached_summaries", lambda snapshots: {})
    monkeypatch.setattr(app, "heartbeat_send_target", lambda: ("%239", "orch:0.0 (%239)"))
    monkeypatch.setattr(dash, "notify", lambda *args, **kwargs: None)

    def fake_send_text(target, prompt, submit_keys):
        sent["target"] = target
        sent["prompt"] = prompt
        sent["submit_keys"] = submit_keys
        return True, "sent"

    def fake_append(path, event):
        ledger_events.append(event)

    monkeypatch.setattr(app, "send_text_to_pane", fake_send_text)
    monkeypatch.setattr(app, "append_ledger", fake_append)

    asyncio.run(dash._run_heartbeat(force=True))

    assert sent["target"] == "%239"
    assert ledger_events[-1]["target"] == app.ORCH_TARGET
    assert ledger_events[-1]["resolved_target"] == "orch:0.0 (%239)"


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
