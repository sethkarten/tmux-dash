import json

from tmux_dash.orchestration import (
    SessionMonitor,
    StatusConfig,
    append_ledger,
    build_heartbeat_prompt,
    observe_session,
    snapshot_to_dict,
)


def test_observe_session_marks_idle_after_unchanged_threshold() -> None:
    config = StatusConfig(idle_after_secs=10)
    first, history = observe_session("agent", "working\nstep 1", None, config, now=100)
    second, _ = observe_session("agent", "working\nstep 1", history, config, now=115)

    assert first.status == "active"
    assert second.status == "idle"
    assert second.idle_seconds == 15


def test_observe_session_detects_error_output() -> None:
    snapshot, _ = observe_session(
        "agent",
        "Traceback (most recent call last):\nValueError: bad input",
        None,
        StatusConfig(),
        now=100,
    )

    assert snapshot.status == "error"


def test_observe_session_detects_waiting_prompt() -> None:
    snapshot, _ = observe_session(
        "agent",
        "Approve sending this message? [y/N]",
        None,
        StatusConfig(),
        now=100,
    )

    assert snapshot.status == "waiting"


def test_observe_session_keeps_codex_background_jobs_active() -> None:
    config = StatusConfig(idle_after_secs=10)
    raw = "• Working (1h 47m 45s • esc to interrupt) · 2 background jobs"
    _, history = observe_session("agent", raw, None, config, now=100)
    snapshot, _ = observe_session("agent", raw, history, config, now=200)

    assert snapshot.status == "active"
    assert snapshot.reason == "Codex reports active work or background jobs"


def test_observe_session_keeps_pursuing_goal_active() -> None:
    config = StatusConfig(idle_after_secs=10)
    raw = "gpt-5.5 xhigh fast · ~/Research/verifiers  Pursuing goal (3h 6m)"
    _, history = observe_session("agent", raw, None, config, now=100)
    snapshot, _ = observe_session("agent", raw, history, config, now=200)

    assert snapshot.status == "active"


def test_session_monitor_tracks_latest_snapshots() -> None:
    monitor = SessionMonitor(StatusConfig(idle_after_secs=5))

    monitor.observe("agent", "first", now=100)
    snapshot = monitor.observe("agent", "first", now=106)

    assert snapshot.status == "idle"
    assert monitor.latest["agent"] == snapshot


def test_heartbeat_prompt_instructs_orchestrator_to_ask_for_approval() -> None:
    snapshot, _ = observe_session(
        "agent",
        "tests are running\n2 passed",
        None,
        StatusConfig(),
        now=100,
    )

    prompt = build_heartbeat_prompt(
        [snapshot],
        summaries={"agent": "Running tests."},
        labels={"agent": "Test Agent"},
        orch_target="orch:0.0",
        now=100,
    )

    assert "tmux-dash heartbeat update" in prompt
    assert "Test Agent (agent)" in prompt
    assert "ask the user for approval" in prompt
    assert "After approval, send approved messages directly" in prompt


def test_append_ledger_writes_jsonl(tmp_path) -> None:
    snapshot, _ = observe_session("agent", "working", None, StatusConfig(), now=100)
    ledger_path = tmp_path / "tmux-dash" / "orchestrator.jsonl"

    append_ledger(
        ledger_path,
        {
            "event": "heartbeat",
            "snapshots": [snapshot_to_dict(snapshot)],
        },
    )

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "heartbeat"
    assert payload["snapshots"][0]["session"] == "agent"
