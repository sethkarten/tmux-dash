"""High-intensity wall monitor for tmux agent sessions."""

from __future__ import annotations

import asyncio
import os
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

from tmux_dash.app import REFRESH_SECS, SESSION_LABELS, STATUS_CONFIG, capture, get_sessions
from tmux_dash.orchestration import (
    CODEX_ACTIVE_RE,
    SessionMonitor,
    SessionSnapshot,
    format_duration,
)


STATUS_STYLES = {
    "active": "bold green",
    "idle": "bold yellow",
    "waiting": "bold cyan",
    "blocked": "bold magenta",
    "error": "bold red",
    "new": "white",
}
PHASE_STYLES = {
    "NOMINAL": "bold green",
    "WATCH": "bold yellow",
    "ALERT": "bold red",
}
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


def status_counts(entries: Iterable[WallEntry]) -> dict[str, int]:
    counts = {"active": 0, "idle": 0, "waiting": 0, "blocked": 0, "error": 0}
    for entry in entries:
        if entry.snapshot.status in counts:
            counts[entry.snapshot.status] += 1
    return counts


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


def meter(percent: int, width: int = 18) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "[" + ("|" * filled).ljust(width, ".") + f"] {percent:03d}%"


def trim_line(line: str, width: int) -> str:
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
        height: 8;
    }
    #body {
        height: 1fr;
    }
    #overview {
        width: 43%;
        height: 100%;
    }
    #focus {
        width: 37%;
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
        self._select_focus()
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
                )
            )
        return entries

    def _select_focus(self) -> None:
        if not self._entries:
            self._focus_session = None
            return
        if self._focus_session and any(entry.session == self._focus_session for entry in self._entries):
            return
        ranked = sorted(
            self._entries,
            key=lambda entry: (
                STATUS_PRIORITY.get(entry.snapshot.status, 9),
                -entry.snapshot.idle_seconds,
                entry.session,
            ),
        )
        self._focus_session = ranked[0].session

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
        counts = status_counts(self._entries)
        avg_sync = int(sum(entry.sync for entry in self._entries) / len(self._entries)) if self._entries else 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        title = Text()
        title.append("OPS SYNCHRONIZATION WALL\n", style="bold red")
        title.append("TMUX MULTI-AGENT OBSERVATION GRID", style="bold #ffb000")

        stats = Text(justify="right")
        stats.append("PHASE        ", style="dim white")
        stats.append(phase + "\n", style=PHASE_STYLES[phase])
        stats.append("GLOBAL SYNC  ", style="dim white")
        stats.append(meter(avg_sync, 12) + "\n", style="bold green")
        stats.append(f"SESSIONS     {len(self._entries)}\n", style="white")
        stats.append(f"CLOCK        {now}", style="white")

        counts_text = Text()
        counts_text.append(f"ACTIVE {counts['active']} ", style="bold green")
        counts_text.append(f"IDLE {counts['idle']} ", style="bold yellow")
        counts_text.append(f"WAIT {counts['waiting']} ", style="bold cyan")
        counts_text.append(f"BLOCK {counts['blocked']} ", style="bold magenta")
        counts_text.append(f"ERROR {counts['error']}", style="bold red")

        layout = Table.grid(expand=True)
        layout.add_column(ratio=2)
        layout.add_column(ratio=1)
        layout.add_row(title, stats)
        layout.add_row(counts_text, Text("READ ONLY // NO CONTROL SURFACE", style="dim red"))

        return Panel(layout, border_style=PHASE_STYLES[phase], box=box.SQUARE)

    def _render_overview(self) -> RenderableType:
        table = Table(
            expand=True,
            box=box.SQUARE,
            border_style="red",
            header_style="bold #ffb000",
            show_lines=False,
        )
        table.add_column("ID", width=3)
        table.add_column("SESSION", ratio=2)
        table.add_column("STATE", width=8)
        table.add_column("SYNC", ratio=2)
        table.add_column("SIGNAL", ratio=2)

        for idx, entry in enumerate(self._entries, start=1):
            status = entry.snapshot.status
            label = trim_line(entry.label, 24)
            if entry.session == self._focus_session:
                label = "> " + label
            table.add_row(
                f"{idx:02d}",
                Text(label, style="bold white" if entry.session == self._focus_session else "white"),
                Text(status.upper(), style=STATUS_STYLES.get(status, "white")),
                Text(meter(entry.sync, 10), style=STATUS_STYLES.get(status, "white")),
                trim_line(entry.snapshot.reason, 30),
            )

        if not self._entries:
            table.add_row("--", "NO TMUX AGENT SESSIONS", "WAIT", meter(0, 10), "no sessions visible")

        return Panel(table, title="ALL PANES", border_style="red", box=box.SQUARE)

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
        target.append(entry.label, style="bold #ffb000")
        if entry.label != entry.session:
            target.append(f"\n{entry.session}", style="dim white")
        header.add_row(target, Text(status.upper(), style=STATUS_STYLES.get(status, "white")))
        header.add_row(
            Text(entry.snapshot.reason, style="white"),
            Text(meter(entry.sync, 14), style=STATUS_STYLES.get(status, "white")),
        )
        header.add_row(
            Text(f"IDLE {format_duration(entry.snapshot.idle_seconds)}", style="dim #ffb000"),
            Text(f"LINES {entry.snapshot.line_count}", style="dim #ffb000"),
        )

        body = Text()
        body.append("\nRECENT TERMINAL SIGNAL\n", style="bold red")
        for line in tail_lines(entry.snapshot.tail, limit=10, width=86):
            body.append("  " + line + "\n", style="green")

        markers = active_markers(entry.raw)
        if markers:
            body.append("\nACTIVE MARKERS\n", style="bold red")
            for line in markers:
                body.append("  " + line + "\n", style="bold green")

        return Panel(Group(header, body), title="TARGET DETAIL", border_style=STATUS_STYLES.get(status, "red"), box=box.SQUARE)

    def _render_alerts(self) -> RenderableType:
        lines = Text()
        phase = phase_for(self._entries)
        lines.append(f"PHASE {phase}\n", style=PHASE_STYLES[phase])
        lines.append("// ALERT STREAM\n\n", style="bold red")

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
            lines.append("  " + trim_line(detail, 44) + "\n\n", style="dim white")

        if not events:
            lines.append("NO EVENTS ABOVE THRESHOLD\n", style="bold green")

        return Panel(lines, title="ALERTS", border_style=PHASE_STYLES[phase], box=box.SQUARE)

    def _render_footer(self) -> RenderableType:
        state = "PAUSED" if self._paused else "LIVE"
        text = Text()
        text.append(f"{state} ", style="bold yellow" if self._paused else "bold green")
        text.append(f"refresh {format_duration(max(1.0, REFRESH_SECS))} ")
        text.append(f"tick {self._tick} ")
        text.append(" | r refresh | n next target | p pause | q quit", style="dim white")
        return Panel(text, border_style="red", box=box.SQUARE)

    def action_refresh(self) -> None:
        self.run_worker(self._refresh_async(), group="wall-refresh", exclusive=True)

    def action_focus_next(self) -> None:
        if not self._entries:
            return
        sessions = [entry.session for entry in self._entries]
        current = sessions.index(self._focus_session) if self._focus_session in sessions else -1
        self._focus_session = sessions[(current + 1) % len(sessions)]
        self._render()

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        self._render()


def main() -> None:
    EvaWall().run()


if __name__ == "__main__":
    main()
