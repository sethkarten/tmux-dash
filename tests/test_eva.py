from tmux_dash.eva import (
    WallEntry,
    active_markers,
    authority_state,
    background_job_count,
    border_field_lines,
    compact_meter,
    corner_timer_blocks,
    diagnostic_bus_lines,
    digest_stream,
    edge_glyph_lines,
    meter,
    limiter_check_lines,
    moving_signal_line,
    operation_mode,
    phase_for,
    power_state,
    protocol_tape_lines,
    protocol_log_lines,
    protocol_eta,
    radial_scanner_lines,
    scanline,
    dense_register_lines,
    sync_scope_lines,
    sync_flux,
    sync_lattice_lines,
    sync_percent,
    tail_lines,
    tri_core_votes,
    trim_art_line,
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


def test_compact_meter_clamps_without_label() -> None:
    assert compact_meter(150, width=5) == "|||||"
    assert compact_meter(-10, width=5) == "....."


def test_sync_flux_stays_bounded() -> None:
    entry = make_entry("active")

    assert 0 <= sync_flux(entry, tick=100) <= 100


def test_unit_id_formats_unit_numbers() -> None:
    assert unit_id(3) == "UNIT-03"


def test_scanline_marks_sweep_position() -> None:
    assert scanline(6, tick=2) == ".=>=.."


def test_moving_signal_line_marks_packets() -> None:
    line = moving_signal_line(12, tick=1, packets=2)

    assert len(line) == 12
    assert ">" in line


def test_edge_glyph_lines_have_motion_markers() -> None:
    lines = edge_glyph_lines([make_entry("active")], tick=1, count=3, width=8)

    assert len(lines) == 3
    assert all(len(line) == 10 for line in lines)


def test_trim_art_line_preserves_scope_spacing() -> None:
    assert trim_art_line("A   B", 8) == "A   B   "


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


def test_diagnostic_bus_lines_include_each_unit() -> None:
    entry = make_entry("active")
    lines = diagnostic_bus_lines([entry], tick=1)

    assert lines[0].startswith("SUBSYSTEM BUS")
    assert "UNIT-01" in lines[1]
    assert "LINK" in lines[1]


def test_border_field_lines_and_register_noise_fill_space() -> None:
    entry = make_entry("active")

    field = border_field_lines([entry], tick=1, count=3)
    registers = dense_register_lines(digest_stream([entry]), tick=1, count=2, width=60, prefix="TST")

    assert field[0].startswith("BORDER FIELD")
    assert len(field) == 3
    assert registers[0].startswith("TST-00")
    assert len(registers) == 2


def test_sync_lattice_lines_include_power_and_bus_rows() -> None:
    entry = make_entry("active")
    lines = sync_lattice_lines(entry, tick=1)

    assert lines[0].startswith("LINK LATTICE")
    assert "POWER" in lines[1]
    assert lines[2].startswith("BUS-00")


def test_limiter_check_lines_fill_unit_diagnostics() -> None:
    entry = make_entry("active")
    lines = limiter_check_lines(entry, tick=1, count=4)

    assert lines[0].startswith("LIMITER CHECKLIST")
    assert len(lines) == 4


def test_protocol_log_lines_prioritize_errors() -> None:
    error = make_entry("error")
    active = make_entry("active")

    lines = protocol_log_lines([active, error], tick=1)

    assert "ERROR" in lines[0]


def test_protocol_tape_lines_repeats_log_to_target_count() -> None:
    lines = protocol_tape_lines([make_entry("active")], tick=1, count=4)

    assert len(lines) == 4
    assert all(line.startswith((">>", "::")) for line in lines)


def test_radial_scanner_lines_include_unit_and_sync() -> None:
    lines = radial_scanner_lines(make_entry("active"), tick=1, width=40, height=11)

    assert len(lines) == 11
    assert all(len(line) == 40 for line in lines)
    assert any("UNIT LOCK" in line for line in lines)
    assert any("PILOT SYNC" in line for line in lines)


def test_sync_scope_lines_show_flux_and_packets() -> None:
    lines = sync_scope_lines([make_entry("active")], tick=1, count=2, width=40)

    assert len(lines) == 2
    assert any("%" in line and ">" in line for line in lines)


def test_corner_timer_blocks_include_heartbeat_and_aux() -> None:
    entry = make_entry("active")
    blocks = corner_timer_blocks([entry], tick=1, now=590)

    assert any("TIME REMAINING" in line for line in blocks)
    assert any("AUXILIARY" in line for line in blocks)


def test_wall_sessions_excludes_current_tmux_session() -> None:
    sessions = wall_sessions(["orch", "programBench", "tmux-eva"], "tmux-eva")

    assert sessions == ["orch", "programBench"]
