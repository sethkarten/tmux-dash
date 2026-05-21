"""High-intensity wall monitor for tmux agent sessions."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable

from rich import box
from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from tmux_dash.app import (
    HEARTBEAT_ENABLED,
    HEARTBEAT_SECS,
    REFRESH_SECS,
    SESSION_LABELS,
    STATUS_CONFIG,
    capture,
    get_sessions,
)
from tmux_dash.orchestration import (
    CODEX_ACTIVE_RE,
    SessionMonitor,
    SessionSnapshot,
    format_duration,
)


STATUS_STYLES: dict[str, str] = {
    "active": "bold #39ff14",
    "idle": "bold #ffb000",
    "waiting": "bold #00f5ff",
    "blocked": "bold #ff5a00",
    "error": "bold reverse #ff2400",
    "new": "white",
}
PHASE_STYLES: dict[str, str] = {
    "NOMINAL": "bold #39ff14",
    "WATCH": "bold #ffb000",
    "ALERT": "bold reverse #ff2400",
}
PANEL_RED = "#d00000"
HOT_RED = "#ff2400"
ORANGE = "#ffb000"
GREEN = "#39ff14"
DIM_GREEN = "#1a8f25"
CREAM = "#ffe6b0"
FOCUS_ROTATE_SECS = 15.0
BG_JOBS_RE = re.compile(r"(?:(\d+)\s+)?backgr(?:ound)?\S*\s+(?:job|terminal)s?", re.IGNORECASE)
CONTROL_ESCAPE_RE = re.compile(r"\\0[0-9]{2}")
STATUS_PRIORITY = {
    "error": 0,
    "blocked": 1,
    "waiting": 2,
    "idle": 3,
    "active": 4,
}


@dataclass(frozen=True)
class WallEntry:
    session: str
    label: str
    snapshot: SessionSnapshot
    raw: str
    sync: int
    background_jobs: int


def status_counts(entries: Iterable[WallEntry]) -> dict[str, int]:
    counts = {"active": 0, "idle": 0, "waiting": 0, "blocked": 0, "error": 0}
    for entry in entries:
        if entry.snapshot.status in counts:
            counts[entry.snapshot.status] += 1
    return counts


def background_job_count(raw: str) -> int:
    current = 0
    for match in BG_JOBS_RE.finditer(raw):
        current = int(match.group(1) or "1")
    return current


def sync_percent(snapshot: SessionSnapshot) -> int:
    status = snapshot.status
    if status == "error":
        return 3
    if status == "blocked":
        return 18
    if status == "waiting":
        return 42
    if status == "idle":
        return max(6, 35 - int(snapshot.idle_seconds // 60))
    if snapshot.reason == "Codex reports active work or background jobs":
        return 99
    if snapshot.changed:
        return 96
    return max(66, 92 - int(snapshot.idle_seconds // 20))


def phase_for(entries: list[WallEntry]) -> str:
    statuses = {entry.snapshot.status for entry in entries}
    if statuses & {"error", "blocked"}:
        return "ALERT"
    if statuses & {"waiting", "idle"}:
        return "WATCH"
    return "NOMINAL"


def operation_mode(entries: list[WallEntry]) -> str:
    statuses = {entry.snapshot.status for entry in entries}
    if "error" in statuses:
        return "FAULT CASCADE"
    if "blocked" in statuses:
        return "COMMAND INTERRUPT"
    if statuses & {"waiting", "idle"}:
        return "OBSERVATION WATCH"
    return "NORMAL OPERATION"


def meter(percent: int, width: int = 18) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "[" + ("|" * filled).ljust(width, ".") + f"] {percent:03d}%"


def compact_meter(percent: int, width: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return ("|" * filled).ljust(width, ".")


def hazard_bar(width: int = 26, tick: int = 0) -> str:
    if width <= 0:
        return ""
    pattern = "//" if tick % 2 == 0 else "\\\\"
    return (pattern * ((width // len(pattern)) + 1))[:width]


def scanline(width: int = 42, tick: int = 0) -> str:
    if width <= 2:
        return ">"
    pos = tick % width
    chars = ["."] * width
    chars[pos] = ">"
    if pos > 0:
        chars[pos - 1] = "="
    if pos + 1 < width:
        chars[pos + 1] = "="
    return "".join(chars)


def digest_stream(entries: list[WallEntry], fallback: str = "OPS") -> str:
    stream = "".join(entry.snapshot.digest for entry in entries)
    return stream or fallback * 32


def dense_register_lines(seed: str, tick: int, count: int, width: int = 84, prefix: str = "REG") -> list[str]:
    source = seed or "0" * 32
    lines: list[str] = []
    for row in range(max(0, count)):
        start = (tick + row * 9) % len(source)
        chunk = (source[start:] + source + source)[:24].upper()
        gates = compact_meter((sum(ord(char) for char in chunk) + row * 13 + tick) % 101, 12)
        trace = scanline(18, tick + row * 2)
        lines.append(trim_line(f"{prefix}-{row:02d} {chunk[:8]} {chunk[8:16]} {chunk[16:24]} {gates} {trace}", width))
    return lines


def border_field_lines(entries: list[WallEntry], tick: int, count: int, width: int = 84) -> list[str]:
    if count <= 0:
        return []
    lines = ["BORDER FIELD CARTOGRAPHY // SYNC PHASE MAP"]
    active_entries = entries or [
        WallEntry(
            session="none",
            label="NO UNIT",
            snapshot=SessionSnapshot("none", "waiting", "no signal", 0, False, 0, "", "0" * 64, 0),
            raw="",
            sync=0,
            background_jobs=0,
        )
    ]
    for row in range(count - 1):
        entry = active_entries[row % len(active_entries)]
        label = trim_line(entry.label, 14).ljust(14)
        phase = (entry.sync + tick + row * 11) % 101
        left = compact_meter(phase, 16)
        right = compact_meter(100 - phase, 16)
        lines.append(trim_line(f"FIELD-{row:02d} {label} {left} CORE {phase:03d} BORDER {right}", width))
    return lines


def limiter_check_lines(entry: WallEntry, tick: int, count: int, width: int = 64) -> list[str]:
    checks = [
        "COGNITION BUS",
        "SANDBOX LINK",
        "TOKEN FEED",
        "AUX PROCESS",
        "PROMPT BUFFER",
        "RESULT TRACE",
        "APPROVAL LOCK",
        "ROLLBACK GATE",
        "NETWORK SEAL",
        "HEARTBEAT BUS",
        "LOG DIGEST",
        "PANE CAPTURE",
    ]
    lines = ["LIMITER CHECKLIST // UNIT INTERNALS"]
    for row in range(max(0, count - 1)):
        name = checks[row % len(checks)]
        level = (entry.sync + len(name) * 7 + tick + row * 5) % 101
        state = "GREEN" if level >= 55 and entry.snapshot.status == "active" else "WATCH" if level >= 24 else "CUT"
        lines.append(trim_line(f"{name.ljust(14)} {state.ljust(5)} {compact_meter(level, 16)} {level:03d}", width))
    return lines


def protocol_tape_lines(entries: list[WallEntry], tick: int, count: int, width: int = 44) -> list[str]:
    base = protocol_log_lines(entries, tick, limit=max(1, count))
    lines: list[str] = []
    for idx in range(max(0, count)):
        line = base[idx % len(base)]
        prefix = ">>" if (idx + tick) % 3 == 0 else "::"
        lines.append(trim_line(f"{prefix} {line}", width))
    return lines


def trim_line(line: str, width: int) -> str:
    line = CONTROL_ESCAPE_RE.sub(" ", line)
    line = "".join(char if ord(char) >= 32 else " " for char in line)
    line = " ".join(line.strip().split())
    if len(line) <= width:
        return line
    return line[: max(0, width - 3)].rstrip() + "..."


def tail_lines(text: str, limit: int = 8, width: int = 86) -> list[str]:
    lines = [trim_line(line, width) for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def active_markers(raw: str) -> list[str]:
    lines = [trim_line(line, 78) for line in raw.splitlines() if CODEX_ACTIVE_RE.search(line)]
    return lines[-3:]


def unit_id(index: int) -> str:
    return f"UNIT-{index:02d}"


def authority_state(snapshot: SessionSnapshot) -> str:
    if snapshot.status == "waiting":
        return "AUTH WAIT"
    if snapshot.status == "blocked":
        return "BORDER LOCK"
    if snapshot.status == "error":
        return "FAULT"
    if snapshot.status == "idle":
        return "LOW SYNC"
    return "CLEAR"


def power_state(entry: WallEntry) -> str:
    if entry.background_jobs:
        return f"AUX-{entry.background_jobs}"
    if entry.snapshot.status == "idle":
        return "RESERVE"
    if entry.snapshot.status in {"blocked", "error"}:
        return "CUT"
    return "DIRECT"


def tri_core_votes(entry: WallEntry) -> list[tuple[str, str, str]]:
    status = entry.snapshot.status
    logic = {
        "active": "TRACK",
        "idle": "STALE",
        "waiting": "AUTH",
        "blocked": "LOCK",
        "error": "FAULT",
    }.get(status, "UNKNOWN")
    ops = {
        "active": "RUN",
        "idle": "WATCH",
        "waiting": "HOLD",
        "blocked": "HALT",
        "error": "ABORT",
    }.get(status, "HOLD")
    link = f"SYNC {entry.sync:03d}"
    return [
        ("CORE-A LOGIC", logic, STATUS_STYLES.get(status, "white")),
        ("CORE-B OPS", ops, STATUS_STYLES.get(status, "white")),
        ("CORE-C LINK", link, STATUS_STYLES.get(status, "white")),
    ]


def protocol_eta(now: float, heartbeat_enabled: bool = HEARTBEAT_ENABLED, heartbeat_secs: float = HEARTBEAT_SECS) -> str:
    if not heartbeat_enabled or heartbeat_secs <= 0:
        return "COMMAND PROTOCOL OFFLINE"
    remaining = int(heartbeat_secs - (now % heartbeat_secs))
    minutes, seconds = divmod(max(0, remaining), 60)
    return f"NEXT COMMAND PROTOCOL T-{minutes:02d}:{seconds:02d}"


def signal_wave(snapshot: SessionSnapshot, width: int = 44, rows: int = 7) -> list[str]:
    source = snapshot.tail or snapshot.digest
    values = [ord(char) for char in source if not char.isspace()]
    if not values:
        values = [0]
    bars: list[str] = []
    for row in range(rows):
        start = (row * 13) % len(values)
        sample = values[start : start + width]
        if len(sample) < width:
            sample += values[: width - len(sample)]
        bar = "".join("#" if value % 10 > 4 else "." for value in sample[:width])
        bars.append(f"{row:02d} {bar}")
    return bars


def power_grid(entries: list[WallEntry]) -> dict[str, int]:
    counts = status_counts(entries)
    return {
        "units": len(entries),
        "active": counts["active"],
        "waiting_auth": counts["waiting"],
        "faults": counts["blocked"] + counts["error"],
        "background_jobs": sum(entry.background_jobs for entry in entries),
        "avg_sync": int(sum(entry.sync for entry in entries) / len(entries)) if entries else 0,
    }


def diagnostic_bus_lines(entries: list[WallEntry], tick: int, width: int = 82) -> list[str]:
    if not entries:
        return ["NO UNIT TELEMETRY AVAILABLE"]
    lines = ["SUBSYSTEM BUS // PLUG DEPTH // BORDER INTEGRITY"]
    for idx, entry in enumerate(entries, start=1):
        label = trim_line(entry.label, 18).ljust(18)
        border = authority_state(entry.snapshot).ljust(11)
        power = power_state(entry).ljust(8)
        digest = entry.snapshot.digest[:8].upper()
        pulse = scanline(18, tick + idx)
        line = (
            f"{unit_id(idx)} {label} LINK {compact_meter(entry.sync, 8)} "
            f"PWR {power} BORDER {border} SIG {digest} {pulse}"
        )
        lines.append(trim_line(line, width))
    return lines


def sync_lattice_lines(entry: WallEntry, tick: int, width: int = 62) -> list[str]:
    digest = entry.snapshot.digest or "0"
    seeds = [ord(char) for char in digest]
    if not seeds:
        seeds = [0]
    lines = [
        "LINK LATTICE // INTERNAL BUS",
        f"POWER {power_state(entry)}  BORDER {authority_state(entry.snapshot)}  AUX {entry.background_jobs}",
    ]
    for row in range(8):
        seed = seeds[row % len(seeds)]
        level = (entry.sync + seed + tick + row * 7) % 101
        scan = scanline(22, tick + row)
        line = f"BUS-{row:02d} {compact_meter(level, 12)} {level:03d}% {scan}"
        lines.append(trim_line(line, width))
    return lines


def protocol_log_lines(entries: list[WallEntry], tick: int, limit: int = 10) -> list[str]:
    ranked = sorted(
        entries,
        key=lambda entry: (
            STATUS_PRIORITY.get(entry.snapshot.status, 9),
            -entry.background_jobs,
            entry.session,
        ),
    )
    lines: list[str] = []
    for offset, entry in enumerate(ranked[:limit]):
        stamp = f"T+{(tick * 5 + offset * 17) % 600:03d}"
        status = entry.snapshot.status.upper().ljust(7)
        label = trim_line(entry.label, 18).ljust(18)
        detail = trim_line(f"{power_state(entry)} {authority_state(entry.snapshot)} {entry.snapshot.reason}", 44)
        lines.append(f"{stamp} {status} {label} {detail}")
    if not lines:
        lines.append("T+000 WAIT    NO UNITS            NO SIGNAL")
    return lines


def current_tmux_session() -> str | None:
    if "TMUX" not in os.environ:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    session = result.stdout.strip()
    return session or None


def wall_sessions(sessions: Iterable[str], current_session: str | None) -> list[str]:
    return [session for session in sessions if session != current_session]


class EvaWall(App[None]):
    """Read-only second-monitor wallboard for tmux orchestration."""

    TITLE = "tmux-eva"
    CSS = """
    Screen {
        background: #080000;
        color: #ffefcf;
    }
    #frame {
        height: 100%;
        width: 100%;
    }
    #header {
        height: 10;
    }
    #body {
        height: 1fr;
    }
    #overview {
        width: 45%;
        height: 100%;
    }
    #focus {
        width: 35%;
        height: 100%;
    }
    #alerts {
        width: 20%;
        height: 100%;
    }
    #footerbar {
        height: 4;
    }
    Static {
        padding: 0;
    }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("n", "focus_next", "Next"),
        ("p", "toggle_pause", "Pause"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._monitor = SessionMonitor(STATUS_CONFIG)
        self._entries: list[WallEntry] = []
        self._focus_session: str | None = None
        self._paused = False
        self._tick = 0
        self._last_focus_rotation = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="frame"):
            yield Static(id="header")
            with Horizontal(id="body"):
                yield Static(id="overview")
                yield Static(id="focus")
                yield Static(id="alerts")
            yield Static(id="footerbar")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(max(1.0, REFRESH_SECS), self._scheduled_refresh)
        self._scheduled_refresh()

    def _scheduled_refresh(self) -> None:
        if self._paused:
            self._render()
            return
        self.run_worker(self._refresh_async(), group="wall-refresh", exclusive=True)

    async def _refresh_async(self) -> None:
        entries = await asyncio.to_thread(self._collect_entries)
        self._entries = entries
        self._tick += 1
        self._select_focus(time.time())
        self._render()

    def _collect_entries(self) -> list[WallEntry]:
        entries: list[WallEntry] = []
        for session in wall_sessions(get_sessions(), current_tmux_session()):
            raw = capture(session, 100)
            snapshot = self._monitor.observe(session, raw)
            label = SESSION_LABELS.get(session, session)
            entries.append(
                WallEntry(
                    session=session,
                    label=label,
                    snapshot=snapshot,
                    raw=raw,
                    sync=sync_percent(snapshot),
                    background_jobs=background_job_count(raw),
                )
            )
        return entries

    def _select_focus(self, now: float) -> None:
        if not self._entries:
            self._focus_session = None
            return
        sessions = [entry.session for entry in self._entries]
        if self._focus_session not in sessions:
            ranked = sorted(
                self._entries,
                key=lambda entry: (
                    STATUS_PRIORITY.get(entry.snapshot.status, 9),
                    -entry.snapshot.idle_seconds,
                    entry.session,
                ),
            )
            self._focus_session = ranked[0].session
            self._last_focus_rotation = now
            return
        if now - self._last_focus_rotation >= FOCUS_ROTATE_SECS:
            current = sessions.index(self._focus_session)
            self._focus_session = sessions[(current + 1) % len(sessions)]
            self._last_focus_rotation = now

    def _focused_entry(self) -> WallEntry | None:
        for entry in self._entries:
            if entry.session == self._focus_session:
                return entry
        return self._entries[0] if self._entries else None

    def _render(self) -> None:
        self.query_one("#header", Static).update(self._render_header())
        self.query_one("#overview", Static).update(self._render_overview())
        self.query_one("#focus", Static).update(self._render_focus())
        self.query_one("#alerts", Static).update(self._render_alerts())
        self.query_one("#footerbar", Static).update(self._render_footer())

    def _render_header(self) -> RenderableType:
        phase = phase_for(self._entries)
        mode = operation_mode(self._entries)
        counts = status_counts(self._entries)
        grid = power_grid(self._entries)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        title = Text()
        title.append("OPS SYNCHRONIZATION WALL\n", style=f"bold {HOT_RED}")
        title.append(mode + "\n", style=PHASE_STYLES[phase])
        title.append("MULTI-AGENT OBSERVATION / COMMAND READOUT", style=f"bold {ORANGE}")

        stats = Text(justify="right")
        stats.append("PHASE        ", style="dim white")
        stats.append(phase + "\n", style=PHASE_STYLES[phase])
        stats.append("GLOBAL SYNC  ", style="dim white")
        stats.append(meter(grid["avg_sync"], 12) + "\n", style=f"bold {GREEN}")
        stats.append(f"UNITS        {grid['units']}\n", style="white")
        stats.append(f"AUX JOBS     {grid['background_jobs']}\n", style=f"bold {ORANGE}")
        stats.append(f"CLOCK        {now}", style="white")

        counts_text = Text()
        counts_text.append(hazard_bar(14, self._tick) + " ", style=f"bold {HOT_RED}")
        counts_text.append(f"ACTIVE {counts['active']} ", style=STATUS_STYLES["active"])
        counts_text.append(f"IDLE {counts['idle']} ", style=STATUS_STYLES["idle"])
        counts_text.append(f"WAIT {counts['waiting']} ", style=STATUS_STYLES["waiting"])
        counts_text.append(f"LOCK {counts['blocked']} ", style=STATUS_STYLES["blocked"])
        counts_text.append(f"FAULT {counts['error']} ", style=STATUS_STYLES["error"])
        counts_text.append(hazard_bar(14, self._tick + 1), style=f"bold {HOT_RED}")

        layout = Table.grid(expand=True)
        layout.add_column(ratio=2)
        layout.add_column(ratio=1)
        layout.add_row(title, stats)
        layout.add_row(counts_text, Text(protocol_eta(time.time()), style=f"bold {ORANGE}"))
        layout.add_row(Text(scanline(64, self._tick), style=f"dim {DIM_GREEN}"), Text("READ ONLY // NO CONTROL SURFACE", style="dim red"))

        return Panel(layout, border_style=PHASE_STYLES[phase], box=box.DOUBLE)

    def _render_overview(self) -> RenderableType:
        table = Table(
            expand=True,
            box=box.SQUARE,
            border_style=PANEL_RED,
            header_style=f"bold {ORANGE}",
            show_lines=False,
        )
        table.add_column("UNIT", width=7)
        table.add_column("SESSION", ratio=2)
        table.add_column("STATE", width=8)
        table.add_column("SYNC", ratio=2)
        table.add_column("POWER", width=8)
        table.add_column("BORDER", ratio=2)

        for idx, entry in enumerate(self._entries, start=1):
            status = entry.snapshot.status
            label = trim_line(entry.label, 24)
            if entry.session == self._focus_session:
                label = "> " + label
            table.add_row(
                unit_id(idx),
                Text(label, style="bold white" if entry.session == self._focus_session else "white"),
                Text(status.upper(), style=STATUS_STYLES.get(status, "white")),
                Text(meter(entry.sync, 10), style=STATUS_STYLES.get(status, "white")),
                Text(power_state(entry), style=f"bold {ORANGE}" if entry.background_jobs else "white"),
                trim_line(authority_state(entry.snapshot), 22),
            )

        if not self._entries:
            table.add_row("UNIT-00", "NO TMUX AGENT SESSIONS", "WAIT", meter(0, 10), "OFFLINE", "NO SIGNAL")

        bus = Text()
        bus.append("\n" + hazard_bar(58, self._tick) + "\n", style=f"bold {HOT_RED}")
        for idx, line in enumerate(diagnostic_bus_lines(self._entries, self._tick)):
            style = f"bold {ORANGE}" if idx == 0 else f"dim {CREAM}"
            bus.append(line + "\n", style=style)
        bus.append(hazard_bar(58, self._tick + 1) + "\n", style=f"bold {HOT_RED}")
        for idx, line in enumerate(border_field_lines(self._entries, self._tick, count=12)):
            style = f"bold {ORANGE}" if idx == 0 else f"dim {DIM_GREEN}"
            bus.append(line + "\n", style=style)
        bus.append(hazard_bar(58, self._tick + 2) + "\n", style=f"bold {HOT_RED}")
        bus.append("LOW-LEVEL REGISTER TRACE\n", style=f"bold {ORANGE}")
        for line in dense_register_lines(digest_stream(self._entries), self._tick, count=12, prefix="MCR"):
            bus.append(line + "\n", style=f"dim {CREAM}")

        return Panel(Group(table, bus), title="UNIT MATRIX", border_style=PANEL_RED, box=box.SQUARE)

    def _render_focus(self) -> RenderableType:
        entry = self._focused_entry()
        if entry is None:
            return Panel(
                Align.center(Text("NO TARGET", style="bold red"), vertical="middle"),
                title="TARGET DETAIL",
                border_style="red",
                box=box.SQUARE,
            )

        status = entry.snapshot.status
        header = Table.grid(expand=True)
        header.add_column(ratio=2)
        header.add_column(ratio=1)
        target = Text()
        target.append(entry.label, style=f"bold {ORANGE}")
        if entry.label != entry.session:
            target.append(f"\n{entry.session}", style="dim white")
        header.add_row(target, Text(status.upper(), style=STATUS_STYLES.get(status, "white")))
        header.add_row(
            Text(entry.snapshot.reason, style="white"),
            Text(meter(entry.sync, 14), style=STATUS_STYLES.get(status, "white")),
        )
        header.add_row(
            Text(f"IDLE {format_duration(entry.snapshot.idle_seconds)}", style=f"dim {ORANGE}"),
            Text(f"LINES {entry.snapshot.line_count}  AUX {entry.background_jobs}", style=f"dim {ORANGE}"),
        )

        body = Text()
        body.append("\n")
        for idx, line in enumerate(sync_lattice_lines(entry, self._tick)):
            style = f"bold {ORANGE}" if idx == 0 else f"dim {CREAM}"
            body.append("  " + line + "\n", style=style)

        body.append("\n")
        for idx, line in enumerate(limiter_check_lines(entry, self._tick, count=12)):
            style = f"bold {ORANGE}" if idx == 0 else f"dim {CREAM}"
            body.append("  " + line + "\n", style=style)

        body.append("\nPILOT LINK WAVEFORM\n", style=f"bold {HOT_RED}")
        for line in signal_wave(entry.snapshot, width=52, rows=12):
            body.append("  " + line + "\n", style=f"bold {DIM_GREEN}")

        body.append("\nUNIT MEMORY STRIPE\n", style=f"bold {HOT_RED}")
        for line in dense_register_lines(entry.snapshot.digest, self._tick, count=8, width=64, prefix="STR"):
            body.append("  " + line + "\n", style=f"dim {CREAM}")

        body.append("\nRECENT TERMINAL SIGNAL\n", style=f"bold {HOT_RED}")
        for line in tail_lines(entry.snapshot.tail, limit=10, width=86):
            body.append("  " + line + "\n", style=GREEN)

        markers = active_markers(entry.raw)
        if markers:
            body.append("\nACTIVE MARKERS\n", style=f"bold {HOT_RED}")
            for line in markers:
                body.append("  " + line + "\n", style=f"bold {GREEN}")

        return Panel(Group(header, body), title="UNIT DIAGNOSTIC", border_style=STATUS_STYLES.get(status, PANEL_RED), box=box.SQUARE)

    def _render_alerts(self) -> RenderableType:
        focused = self._focused_entry()
        lines = Text()
        phase = phase_for(self._entries)
        grid = power_grid(self._entries)
        lines.append("TRI-CORE DECISION\n", style=f"bold {ORANGE}")
        if focused:
            for name, vote, style in tri_core_votes(focused):
                lines.append(name.ljust(13), style="dim white")
                lines.append(vote + "\n", style=style)
        else:
            lines.append("NO TARGET\n", style=STATUS_STYLES["waiting"])

        lines.append("\nPOWER GRID\n", style=f"bold {ORANGE}")
        lines.append(f"UNITS       {grid['units']:02d}\n", style="white")
        lines.append(f"AUX JOBS    {grid['background_jobs']:02d}\n", style=f"bold {ORANGE}")
        lines.append(f"AUTH WAIT   {grid['waiting_auth']:02d}\n", style=STATUS_STYLES["waiting"])
        lines.append(f"FAULTS      {grid['faults']:02d}\n", style=STATUS_STYLES["error"] if grid["faults"] else "white")

        lines.append("\nCOMMAND LOG\n", style=f"bold {ORANGE}")
        for line in protocol_log_lines(self._entries, self._tick, limit=9):
            lines.append(trim_line(line, 48) + "\n", style=f"dim {CREAM}")

        lines.append("\nPROTOCOL TAPE\n", style=f"bold {ORANGE}")
        for line in protocol_tape_lines(self._entries, self._tick, count=10, width=48):
            lines.append(line + "\n", style=f"dim {CREAM}")

        lines.append("\nCORE REGISTER NOISE\n", style=f"bold {ORANGE}")
        for line in dense_register_lines(digest_stream(self._entries), self._tick, count=10, width=48, prefix="AUX"):
            lines.append(line + "\n", style=f"dim {DIM_GREEN}")

        lines.append("\n" + hazard_bar(30, self._tick) + "\n", style=f"bold {HOT_RED}")
        lines.append(f"PHASE {phase}\n", style=PHASE_STYLES[phase])
        lines.append("// ALERT CASCADE\n\n", style=f"bold {HOT_RED}")

        events: list[tuple[str, str, str]] = []
        for entry in self._entries:
            status = entry.snapshot.status
            if status != "active":
                events.append((status, entry.label, entry.snapshot.reason))
            else:
                markers = active_markers(entry.raw)
                if markers:
                    events.append(("active", entry.label, markers[-1]))

        events.sort(key=lambda item: STATUS_PRIORITY.get(item[0], 9))
        for status, label, detail in events[:12]:
            lines.append(status.upper().ljust(8), style=STATUS_STYLES.get(status, "white"))
            lines.append(trim_line(label, 22) + "\n", style="bold white")
            lines.append("  " + trim_line(detail, 44) + "\n\n", style=f"dim {CREAM}")

        if not events:
            lines.append("NO EVENTS ABOVE THRESHOLD\n", style=f"bold {GREEN}")

        return Panel(lines, title="ALERTS", border_style=PHASE_STYLES[phase], box=box.SQUARE)

    def _render_footer(self) -> RenderableType:
        state = "PAUSED" if self._paused else "LIVE"
        grid = power_grid(self._entries)
        text = Text()
        text.append(f"{state} ", style=STATUS_STYLES["idle"] if self._paused else STATUS_STYLES["active"])
        text.append(f"refresh {format_duration(max(1.0, REFRESH_SECS))} ")
        text.append(f"tick {self._tick} ")
        text.append(protocol_eta(time.time()) + " ", style=f"bold {ORANGE}")
        text.append(f"avg-sync {grid['avg_sync']:03d}% aux {grid['background_jobs']} ")
        text.append(" | r refresh | n next target | p pause | q quit", style="dim white")
        return Panel(text, border_style=PANEL_RED, box=box.SQUARE)

    def action_refresh(self) -> None:
        self.run_worker(self._refresh_async(), group="wall-refresh", exclusive=True)

    def action_focus_next(self) -> None:
        if not self._entries:
            return
        sessions = [entry.session for entry in self._entries]
        current = sessions.index(self._focus_session) if self._focus_session in sessions else -1
        self._focus_session = sessions[(current + 1) % len(sessions)]
        self._last_focus_rotation = time.time()
        self._render()

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        self._render()


def main() -> None:
    EvaWall().run()


if __name__ == "__main__":
    main()
