from tmux_dash.eva import WallEntry, active_markers, meter, phase_for, sync_percent, tail_lines
from tmux_dash.eva import wall_sessions
from tmux_dash.orchestration import SessionSnapshot


def make_snapshot(
    status: str,
    reason: str = "pane changed since last check",
    idle_seconds: float = 0,
    changed: bool = True,
) -> SessionSnapshot:
    return SessionSnapshot(
        session="agent",
        status=status,
        reason=reason,
        idle_seconds=idle_seconds,
        changed=changed,
        line_count=3,
        tail="line 1\nline 2",
        digest="abc",
        timestamp=100,
    )


def make_entry(status: str) -> WallEntry:
    snapshot = make_snapshot(status)
    return WallEntry(
        session="agent",
        label="Agent",
        snapshot=snapshot,
        raw=snapshot.tail,
        sync=sync_percent(snapshot),
    )


def test_phase_for_escalates_errors() -> None:
    assert phase_for([make_entry("active"), make_entry("error")]) == "ALERT"


def test_phase_for_marks_idle_as_watch() -> None:
    assert phase_for([make_entry("active"), make_entry("idle")]) == "WATCH"


def test_sync_percent_keeps_codex_background_jobs_high() -> None:
    snapshot = make_snapshot(
        "active",
        reason="Codex reports active work or background jobs",
        idle_seconds=600,
        changed=False,
    )

    assert sync_percent(snapshot) == 99


def test_meter_clamps_percent() -> None:
    assert meter(150, width=5) == "[|||||] 100%"
    assert meter(-10, width=5) == "[.....] 000%"


def test_tail_lines_trims_recent_nonempty_lines() -> None:
    lines = tail_lines("one\n\n" + ("x" * 20) + "\nthree", limit=2, width=10)

    assert lines == ["xxxxxxx...", "three"]


def test_active_markers_finds_codex_activity() -> None:
    raw = "prompt\nWorking (2m) - 2 background jobs\nother"

    assert active_markers(raw) == ["Working (2m) - 2 background jobs"]


def test_wall_sessions_excludes_current_tmux_session() -> None:
    sessions = wall_sessions(["orch", "programBench", "tmux-eva"], "tmux-eva")

    assert sessions == ["orch", "programBench"]
