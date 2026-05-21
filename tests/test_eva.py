from tmux_dash.eva import (
    WallEntry,
    active_markers,
    authority_state,
    background_job_count,
    meter,
    operation_mode,
    phase_for,
    power_state,
    protocol_eta,
    scanline,
    sync_percent,
    tail_lines,
    tri_core_votes,
    unit_id,
)
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
        background_jobs=0,
    )


def test_phase_for_escalates_errors() -> None:
    assert phase_for([make_entry("active"), make_entry("error")]) == "ALERT"


def test_phase_for_marks_idle_as_watch() -> None:
    assert phase_for([make_entry("active"), make_entry("idle")]) == "WATCH"


def test_operation_mode_names_fault_and_watch_states() -> None:
    assert operation_mode([make_entry("active"), make_entry("blocked")]) == "COMMAND INTERRUPT"
    assert operation_mode([make_entry("waiting")]) == "OBSERVATION WATCH"


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


def test_unit_id_formats_unit_numbers() -> None:
    assert unit_id(3) == "UNIT-03"


def test_scanline_marks_sweep_position() -> None:
    assert scanline(6, tick=2) == ".=>=.."


def test_tail_lines_trims_recent_nonempty_lines() -> None:
    lines = tail_lines("one\n\n" + ("x" * 20) + "\nthree\\012four", limit=2, width=10)

    assert lines == ["xxxxxxx...", "three four"]


def test_active_markers_finds_codex_activity() -> None:
    raw = "prompt\nWorking (2m) - 2 background jobs\nother"

    assert active_markers(raw) == ["Working (2m) - 2 background jobs"]


def test_background_job_count_uses_latest_marker() -> None:
    raw = "Working (1m) - 1 background job\nWorking (2m) - 2 background jobs"

    assert background_job_count(raw) == 2


def test_authority_and_power_states() -> None:
    waiting = make_entry("waiting")
    active = make_entry("active")
    active_with_background = WallEntry(
        session=active.session,
        label=active.label,
        snapshot=active.snapshot,
        raw="Working - 2 background jobs",
        sync=active.sync,
        background_jobs=2,
    )

    assert authority_state(waiting.snapshot) == "AUTH WAIT"
    assert power_state(active_with_background) == "AUX-2"


def test_tri_core_votes_reflect_status_and_sync() -> None:
    entry = make_entry("error")

    votes = tri_core_votes(entry)

    assert votes[0][1] == "FAULT"
    assert votes[1][1] == "ABORT"
    assert votes[2][1] == f"SYNC {entry.sync:03d}"


def test_protocol_eta_handles_disabled_and_countdown() -> None:
    assert protocol_eta(100, heartbeat_enabled=False, heartbeat_secs=600) == "COMMAND PROTOCOL OFFLINE"
    assert protocol_eta(590, heartbeat_enabled=True, heartbeat_secs=600) == "NEXT COMMAND PROTOCOL T-00:10"


def test_wall_sessions_excludes_current_tmux_session() -> None:
    sessions = wall_sessions(["orch", "programBench", "tmux-eva"], "tmux-eva")

    assert sessions == ["orch", "programBench"]
