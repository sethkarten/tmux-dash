"""Restore the user's Codex orchestrator tmux workspace."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ORCH_TARGET = "orch:0.0"
ORCH_PANE_OPT = "@tmux_dash_orch_pane"
SWAPPED_SESSION_OPT = "@tmux_dash_swapped_session"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "xhigh"
DEFAULT_EXIT_LOG = "~/.local/state/tmux-dash/tmux-dash-exits.log"


@dataclass(frozen=True)
class CodexSession:
    name: str
    cwd: str
    session_id: str


ORCH_SESSION = CodexSession(
    "orch",
    "/Users/milkkarten/Planning",
    "019e4735-8270-7eb2-9f88-7cabcd53c4f7",
)

AGENT_SESSIONS = (
    CodexSession(
        "programBench",
        "/Users/milkkarten/Research/verifiers",
        "019e277d-3bd1-7683-9749-9c375f760168",
    ),
    CodexSession(
        "emulatorBench",
        "/Users/milkkarten/Research/emulator",
        "019e2a06-3661-7463-844f-ea06cbd4dce9",
    ),
    CodexSession(
        "FLE",
        "/Users/milkkarten/Research/continual-harness",
        "019e2eeb-3046-7bd0-b7ed-5034699908ba",
    ),
    CodexSession(
        "balrogDAgger",
        "/Users/milkkarten/Research/balrog-dagger",
        "019e3ec1-4383-78f0-8443-f063f11c652f",
    ),
    CodexSession(
        "balrogRL",
        "/Users/milkkarten/Planning",
        "019e2772-9f7d-7973-b2ce-e29a139e3f7a",
    ),
    CodexSession(
        "chessDAgger",
        "/Users/milkkarten/Research/pokejax",
        "019e27e1-0ee7-7041-ba18-e9d66970ab8e",
    ),
    CodexSession(
        "scrapeEnv",
        "/Users/milkkarten/Planning",
        "019e4c48-0262-7461-8155-ab657931226c",
    ),
)


def _tmux(args: Sequence[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=capture,
        check=False,
        text=True,
        timeout=5,
    )


def _tmux_out(args: Sequence[str]) -> str:
    result = _tmux(args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _tmux_check(args: Sequence[str]) -> bool:
    return _tmux(args).returncode == 0


def _codex_resume_command(session_id: str, model: str, effort: str) -> str:
    return shlex.join(
        [
            "codex",
            "resume",
            "-m",
            model,
            "-c",
            f"reasoning_effort={effort}",
            "--yolo",
            session_id,
        ]
    )


def _dashboard_command(exit_log: Path) -> str:
    return (
        "tmux-dash; "
        "code=$?; "
        "stamp=$(date '+%Y-%m-%dT%H:%M:%S%z'); "
        f"printf 'tmux-dash exited %s at %s\\n' \"$code\" \"$stamp\" | tee -a {shlex.quote(str(exit_log))}; "
        'shell=${SHELL:-/bin/sh}; exec "$shell" -l'
    )


def _pane_id(target: str) -> str | None:
    pane = _tmux_out(["display-message", "-p", "-t", target, "#{pane_id}"])
    return pane or None


def _pane_exists(target: str) -> bool:
    return _pane_id(target) is not None


def _find_pane_by_resume_id(session_id: str) -> str | None:
    panes = _tmux_out(["list-panes", "-a", "-F", "#{pane_id} #{pane_start_command}"])
    for line in panes.splitlines():
        if session_id in line:
            return line.split(" ", 1)[0]
    return None


def _ensure_codex_session(session: CodexSession, model: str, effort: str) -> None:
    if _tmux_check(["has-session", "-t", session.name]):
        print(f"exists: {session.name}")
        return

    command = _codex_resume_command(session.session_id, model, effort)
    result = _tmux(["new-session", "-d", "-s", session.name, "-c", session.cwd, command])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown tmux error"
        raise RuntimeError(f"could not create {session.name}: {detail}")
    print(f"created: {session.name} -> {session.session_id}")


def _restore_orchestrator_pane() -> str:
    actual_orch_pane = _find_pane_by_resume_id(ORCH_SESSION.session_id)
    saved_orch_pane = _tmux_out(["show-options", "-gqv", ORCH_PANE_OPT])
    current_orch_pane = _pane_id(ORCH_TARGET)

    if actual_orch_pane and _pane_exists(actual_orch_pane):
        orch_pane = actual_orch_pane
    elif saved_orch_pane and _pane_exists(saved_orch_pane):
        orch_pane = saved_orch_pane
    elif current_orch_pane:
        orch_pane = current_orch_pane
    else:
        raise RuntimeError(f"could not resolve {ORCH_TARGET}")

    if current_orch_pane != orch_pane:
        result = _tmux(["swap-pane", "-d", "-s", orch_pane, "-t", ORCH_TARGET])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown tmux error"
            raise RuntimeError(f"could not restore orchestrator pane {orch_pane}: {detail}")

    _tmux(["set-option", "-gq", ORCH_PANE_OPT, orch_pane])
    _tmux(["set-option", "-guq", SWAPPED_SESSION_OPT])
    return orch_pane


def _ensure_orchestrator(model: str, effort: str, exit_log: Path) -> None:
    _ensure_codex_session(ORCH_SESSION, model, effort)
    orch_pane = _restore_orchestrator_pane()

    pane_count = len(_tmux_out(["list-panes", "-t", "orch:0", "-F", "#{pane_id}"]).splitlines())
    if pane_count < 2:
        exit_log.parent.mkdir(parents=True, exist_ok=True)
        result = _tmux(
            [
                "split-window",
                "-h",
                "-p",
                "38",
                "-t",
                orch_pane,
                "-c",
                ORCH_SESSION.cwd,
                _dashboard_command(exit_log),
            ]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown tmux error"
            raise RuntimeError(f"could not create tmux-dash right pane: {detail}")
        _tmux(["select-pane", "-t", orch_pane])
        print("created: tmux-dash right pane")
    else:
        print("exists: orch dashboard pane(s)")


def restore_workspace(model: str, effort: str, exit_log: Path) -> None:
    for session in AGENT_SESSIONS:
        _ensure_codex_session(session, model, effort)
    _ensure_orchestrator(model, effort, exit_log)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore the Codex orch + tmux-dash tmux workspace.")
    parser.add_argument("--model", default=os.environ.get("CODEX_RESTORE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--effort", default=os.environ.get("CODEX_RESTORE_EFFORT", DEFAULT_EFFORT))
    parser.add_argument(
        "--exit-log",
        default=os.environ.get("TMUX_DASH_EXIT_LOG", DEFAULT_EXIT_LOG),
        help="tmux-dash exit log path used by the persistent right-pane wrapper.",
    )
    parser.add_argument("--attach", action="store_true", help="Attach to the restored orch tmux session.")
    args = parser.parse_args(argv)

    try:
        restore_workspace(args.model, args.effort, Path(args.exit_log).expanduser())
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"restore-codex-orch: {exc}", flush=True)
        return 1

    if args.attach:
        return subprocess.run(["tmux", "attach", "-t", "orch"], check=False).returncode

    print("\nAttach with: tmux attach -t orch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
