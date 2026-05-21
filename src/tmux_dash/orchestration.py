"""Orchestration state detection, heartbeat prompts, and ledger helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StatusConfig:
    idle_after_secs: float = 900.0
    detect_tracebacks: bool = True
    detect_repeated_output: bool = True
    detect_waiting_prompts: bool = True


@dataclass(frozen=True)
class SessionHistory:
    digest: str
    last_changed: float


@dataclass(frozen=True)
class SessionSnapshot:
    session: str
    status: str
    reason: str
    idle_seconds: float
    changed: bool
    line_count: int
    tail: str
    digest: str
    timestamp: float


ERROR_RE = re.compile(
    r"(Traceback \(most recent call last\)|"
    r"\b\w*(Error|Exception):|"
    r"\b(command not found|No such file or directory|segmentation fault|panic:)\b|"
    r"\b(Command failed|failed with exit code|Fatal:|fatal:)\b)",
    re.IGNORECASE,
)
BLOCKED_RE = re.compile(
    r"\b(blocked|cannot proceed|stuck|waiting for user|needs? approval|"
    r"permission denied|authentication failed|rate limit|quota exceeded|"
    r"merge conflict|conflict markers?)\b",
    re.IGNORECASE,
)
WAITING_RE = re.compile(
    r"(\[[yY]/[nN]\]|\([yY]/[nN]\)|"
    r"\b(approve|confirm|continue|proceed)\?\s*$|"
    r"\bpress (enter|return|any key)\b|"
    r"^\s*(>|>>>|\$|%|❯|›)\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
CODEX_ACTIVE_RE = re.compile(
    r"(\bWorking \(|"
    r"\bPursuing goal\b|"
    r"\bWaiting for background terminal\b|"
    r"\bbackground terminal\b|"
    r"\bbackground terminals\b|"
    r"\bbackground job\b|"
    r"\bbackground jobs\b|"
    r"\b\d+\s+backgr)",
    re.IGNORECASE,
)


def observe_session(
    session: str,
    raw: str,
    history: SessionHistory | None,
    config: StatusConfig,
    now: float | None = None,
) -> tuple[SessionSnapshot, SessionHistory]:
    """Classify the current state of a tmux session capture."""

    observed_at = time.time() if now is None else now
    text = raw or ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    changed = history is None or digest != history.digest
    last_changed = observed_at if changed else history.last_changed
    idle_seconds = max(0.0, observed_at - last_changed)
    lines = [line.rstrip() for line in text.splitlines()]
    nonempty = [line for line in lines if line.strip()]
    tail_lines = nonempty[-12:]
    tail = "\n".join(tail_lines)

    status, reason = classify_capture(text, tail_lines, changed, idle_seconds, config)
    snapshot = SessionSnapshot(
        session=session,
        status=status,
        reason=reason,
        idle_seconds=idle_seconds,
        changed=changed,
        line_count=len(nonempty),
        tail=tail,
        digest=digest,
        timestamp=observed_at,
    )
    return snapshot, SessionHistory(digest=digest, last_changed=last_changed)


def classify_capture(
    raw: str,
    tail_lines: list[str],
    changed: bool,
    idle_seconds: float,
    config: StatusConfig,
) -> tuple[str, str]:
    stripped = raw.strip()
    tail = "\n".join(tail_lines)

    if stripped == "(unreachable)":
        return "error", "tmux pane unreachable"
    if not stripped or stripped == "·":
        return "waiting", "no recent pane output"
    if config.detect_tracebacks and ERROR_RE.search(tail):
        return "error", "error-like output in recent pane text"
    if BLOCKED_RE.search(tail):
        return "blocked", "blocked or approval-related text detected"
    if CODEX_ACTIVE_RE.search(tail):
        return "active", "Codex reports active work or background jobs"
    if config.detect_waiting_prompts and WAITING_RE.search(tail):
        return "waiting", "prompt appears to be waiting for input"
    if idle_seconds >= config.idle_after_secs:
        return "idle", f"no pane changes for {format_duration(idle_seconds)}"
    if config.detect_repeated_output and _looks_repetitive(tail_lines):
        return "blocked", "recent output is repeating"
    if changed:
        return "active", "pane changed since last check"
    return "active", "pane unchanged but below idle threshold"


def _looks_repetitive(lines: list[str]) -> bool:
    normalized = [line.strip() for line in lines if line.strip()]
    if len(normalized) < 6:
        return False
    counts: dict[str, int] = {}
    for line in normalized[-10:]:
        counts[line] = counts.get(line, 0) + 1
    return max(counts.values(), default=0) >= 5


class SessionMonitor:
    """Tracks previous captures so status detection can identify idle sessions."""

    def __init__(self, config: StatusConfig):
        self.config = config
        self.histories: dict[str, SessionHistory] = {}
        self.latest: dict[str, SessionSnapshot] = {}

    def observe(self, session: str, raw: str, now: float | None = None) -> SessionSnapshot:
        snapshot, history = observe_session(
            session=session,
            raw=raw,
            history=self.histories.get(session),
            config=self.config,
            now=now,
        )
        self.histories[session] = history
        self.latest[session] = snapshot
        return snapshot


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def snapshot_to_dict(snapshot: SessionSnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    data["idle"] = format_duration(snapshot.idle_seconds)
    data["timestamp_iso"] = _timestamp(snapshot.timestamp)
    return data


def append_ledger(path: str | Path, event: dict[str, Any]) -> None:
    ledger_path = Path(path).expanduser()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def build_heartbeat_prompt(
    snapshots: list[SessionSnapshot],
    summaries: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    orch_target: str = "orch:0.0",
    now: float | None = None,
) -> str:
    observed_at = time.time() if now is None else now
    summaries = summaries or {}
    labels = labels or {}
    lines = [
        "tmux-dash heartbeat update.",
        f"Time: {_timestamp(observed_at)}",
        f"Orchestrator pane: {orch_target}",
        "",
        "You are the orchestrator Codex for this tmux workspace.",
        "Summarize current progress across sessions, call out idle/blocked/error states,",
        "and recommend the next actions. Do not message other sessions yet.",
        "If messages should be sent, ask the user for approval in this pane.",
        "After approval, send approved messages directly to the relevant tmux sessions.",
        "",
        "Observed sessions:",
    ]

    if not snapshots:
        lines.append("- none")
    for snapshot in snapshots:
        label = labels.get(snapshot.session, snapshot.session)
        title = snapshot.session if label == snapshot.session else f"{label} ({snapshot.session})"
        lines.extend(
            [
                f"- {title}",
                f"  status: {snapshot.status}",
                f"  reason: {snapshot.reason}",
                f"  idle: {format_duration(snapshot.idle_seconds)}",
                f"  changed_since_last_check: {str(snapshot.changed).lower()}",
            ]
        )
        summary = summaries.get(snapshot.session)
        if summary:
            lines.append(f"  summary: {_compact(summary, 500)}")
        if snapshot.tail:
            lines.append("  recent_tail:")
            lines.extend(f"    {line}" for line in _compact(snapshot.tail, 900).splitlines())
        lines.append("")

    return "\n".join(lines).rstrip()


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def _compact(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 20:
        return text[:max_chars]
    keep = (max_chars - 15) // 2
    return f"{text[:keep]} ... [truncated] ... {text[-keep:]}"
