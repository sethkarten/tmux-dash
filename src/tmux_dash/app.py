"""tmux multi-agent session dashboard with AI summaries."""

import asyncio
import hashlib
import os
import random
import re
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path
from typing import Any

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Footer, Input, Static

from tmux_dash.orchestration import (
    CODEX_ACTIVE_RE,
    SessionMonitor,
    SessionSnapshot,
    StatusConfig,
    append_ledger,
    build_heartbeat_prompt,
    format_duration,
    snapshot_to_dict,
)

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "tmux-dash" / "config.toml"


def _load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("TMUX_DASH_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if not isinstance(value, list):
        return default
    return [str(item) for item in value]


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _config_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _config_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _config_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _session_labels(value: Any) -> dict[str, str]:
    sessions = _config_dict(value)
    labels: dict[str, str] = {}
    for key, metadata in sessions.items():
        if isinstance(metadata, dict) and "label" in metadata:
            labels[str(key)] = str(metadata["label"])
    return labels


_CONFIG = _load_config()
_ORCH_CONFIG = _config_dict(_CONFIG.get("orchestrator"))
_STATUS_CONFIG = _config_dict(_CONFIG.get("status"))

AGENT_ORDER = _string_list(_CONFIG.get("agent_order"), [])
SUBSESSIONS: dict[str, str] = _string_map(_CONFIG.get("subsessions"))
EXCLUDE: set[str] = set(_string_list(_CONFIG.get("exclude"), ["orch"]))
ORCH_TARGET = str(_ORCH_CONFIG.get("target", _CONFIG.get("orch_target", "orch:0.0")))
SESSION_LABELS = _session_labels(_CONFIG.get("sessions"))

IDLE_AFTER_SECS = _config_float(_ORCH_CONFIG.get("idle_after_secs", _STATUS_CONFIG.get("idle_after_secs")), 900.0)
STATUS_CONFIG = StatusConfig(
    idle_after_secs=IDLE_AFTER_SECS,
    detect_tracebacks=_config_bool(_STATUS_CONFIG.get("detect_tracebacks"), True),
    detect_repeated_output=_config_bool(_STATUS_CONFIG.get("detect_repeated_output"), True),
    detect_waiting_prompts=_config_bool(_STATUS_CONFIG.get("detect_waiting_prompts"), True),
)
ORCH_MONITOR_CONFIG = StatusConfig(
    idle_after_secs=_config_float(_ORCH_CONFIG.get("quiet_secs"), 20.0),
    detect_tracebacks=False,
    detect_repeated_output=False,
    detect_waiting_prompts=False,
)

SUMMARY_TTL  = _config_float(_CONFIG.get("summary_ttl"), 30.0)
REFRESH_SECS = _config_float(_CONFIG.get("refresh_secs"), 5.0)
HEARTBEAT_ENABLED = _config_bool(_ORCH_CONFIG.get("enabled"), False)
HEARTBEAT_SECS = _config_float(_ORCH_CONFIG.get("heartbeat_secs"), 600.0)
HEARTBEAT_LEDGER_PATH = Path(str(_ORCH_CONFIG.get("ledger_path", "~/.local/state/tmux-dash/orchestrator.jsonl"))).expanduser()
_HEARTBEAT_SUBMIT_KEYS = _string_list(_ORCH_CONFIG.get("submit_keys"), [])
if not _HEARTBEAT_SUBMIT_KEYS:
    legacy_submit_key = str(_ORCH_CONFIG.get("submit_key", "Tab"))
    _HEARTBEAT_SUBMIT_KEYS = ["Tab", "Enter"] if legacy_submit_key == "Tab" else [legacy_submit_key]
HEARTBEAT_SUBMIT_KEYS = _HEARTBEAT_SUBMIT_KEYS

CARD_COLORS = ["cyan", "magenta", "green", "yellow", "blue", "red", "white"]
CARD_W, CARD_H = 26, 6  # bounce card dimensions in cells
CARAMELLDANSEN_ASSET = files("tmux_dash.assets").joinpath("caramelldansen.txt")
CARAMELLDANSEN_FRAME_TICKS = 1
CARAMELLDANSEN_STYLE = Style(color=Color.from_rgb(95, 215, 255))
CARAMELLDANSEN_WAVE_STYLE = Style(color=Color.from_rgb(135, 223, 255), dim=True)

_CARAMELLDANSEN_FRAMES: list[list[str]] | None = None

# ── Helpers ───────────────────────────────────────────────────────────────────

_ansi = re.compile(r'\x1b\[[0-9;]*[mGKHFJA-Za-z]|\x1b\(B|\x1b=|\r')

def strip_ansi(text: str) -> str:
    return _ansi.sub('', text)


def _load_ascii_frames(path: Any) -> list[list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    frames: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("--- Frame ") and line.endswith(" ---"):
            if current:
                frames.append(current)
            current = []
        else:
            current.append(line.rstrip())
    if current:
        frames.append(current)

    return [frame for frame in frames if any(row.strip() for row in frame)]


def _get_caramelldansen_frames() -> list[list[str]]:
    global _CARAMELLDANSEN_FRAMES
    if _CARAMELLDANSEN_FRAMES is None:
        _CARAMELLDANSEN_FRAMES = _load_ascii_frames(CARAMELLDANSEN_ASSET)
    return _CARAMELLDANSEN_FRAMES


def _fit_ascii_frame(frame: list[str], max_w: int, max_h: int) -> list[str]:
    if not frame or max_w <= 0 or max_h <= 0:
        return []

    src_h = len(frame)
    src_w = max((len(row) for row in frame), default=0)
    if src_w == 0:
        return []

    x_step = max(1, (src_w + max_w - 1) // max_w)
    y_step = max(1, (src_h + max_h - 1) // max_h)
    rows = [row[::x_step][:max_w] for row in frame[::y_step]]
    return rows[:max_h]


def get_sessions() -> list[str]:
    try:
        r = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2,
        )
        active = set(r.stdout.strip().splitlines())
    except Exception:
        return []
    ordered = [s for s in AGENT_ORDER if s in active]
    extra   = sorted(s for s in active if s not in AGENT_ORDER and s not in EXCLUDE and s not in SUBSESSIONS)
    subs    = sorted(s for s in SUBSESSIONS if s in active)
    return ordered + extra + subs


def capture(session: str, n: int = 50) -> str:
    target = agent_pane_target(session)
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{n}"],
            capture_output=True, text=True, timeout=2,
        )
        clean = strip_ansi(r.stdout)
        lines = [l for l in clean.splitlines() if l.strip()]
        return "\n".join(lines[-n:]) if lines else "·"
    except Exception:
        return "(unreachable)"


def agent_pane_target(session: str) -> str:
    if session.startswith("%") or ":" in session:
        return session
    if _swapped_session() == session:
        return ORCH_TARGET
    main_pane = f"{session}:0.0"
    return main_pane if _pane_exists(main_pane) else session


def _pane_path(target: str) -> str | None:
    path = _tmux_out(["display-message", "-p", "-t", target, "#{pane_current_path}"])
    return path or None


VISIBLE_TERMINAL_OPT = "@tmux_dash_visible_terminal"
VISIBLE_TERMINAL_SESSION_OPT = "@tmux_dash_visible_terminal_session"


def _dashboard_pane() -> str | None:
    pane_id = os.environ.get("TMUX_PANE")
    if pane_id and _pane_exists(pane_id):
        return pane_id
    return None


def _select_dashboard_pane() -> None:
    pane_id = _dashboard_pane()
    if pane_id:
        _tmux(["select-pane", "-t", pane_id])


def _close_visible_terminal() -> None:
    pane_id = _tmux_out(["show-options", "-gqv", VISIBLE_TERMINAL_OPT])
    if pane_id and _pane_exists(pane_id):
        _tmux(["kill-pane", "-t", pane_id])
    _clear_tmux_option(VISIBLE_TERMINAL_OPT)
    _clear_tmux_option(VISIBLE_TERMINAL_SESSION_OPT)


def open_terminal(session: str) -> tuple[bool, str]:
    existing = _tmux_out(["show-options", "-gqv", VISIBLE_TERMINAL_OPT])
    existing_session = _tmux_out(["show-options", "-gqv", VISIBLE_TERMINAL_SESSION_OPT])
    if existing and existing_session == session and _pane_exists(existing):
        _tmux(["select-pane", "-t", existing])
        return True, f"selected existing terminal {existing}"
    _close_visible_terminal()

    if _swapped_session() != session and not switch_to(session):
        return False, f"could not display {session} in {ORCH_TARGET}"

    target = _pane_id(ORCH_TARGET)
    if target is None:
        return False, f"could not resolve {ORCH_TARGET}"

    args = ["split-window", "-d", "-v", "-p", "35", "-P", "-F", "#{pane_id}"]
    path = _pane_path(target)
    if path:
        args.extend(["-c", path])
    args.extend(["-t", target])

    result = _tmux(args)
    if result.returncode != 0:
        return False, result.stderr.strip() or "tmux split-window failed"

    pane_id = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "new pane"
    _set_tmux_option(VISIBLE_TERMINAL_OPT, pane_id)
    _set_tmux_option(VISIBLE_TERMINAL_SESSION_OPT, session)
    _tmux(["select-pane", "-t", pane_id])
    return True, f"opened {pane_id} below {session}"


ORCH_PANE_OPT = "@tmux_dash_orch_pane"
SWAPPED_SESSION_OPT = "@tmux_dash_swapped_session"


def _tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=2)


def _tmux_out(args: list[str]) -> str:
    result = _tmux(args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _set_tmux_option(name: str, value: str) -> None:
    _tmux(["set-option", "-gq", name, value])


def _clear_tmux_option(name: str) -> None:
    _tmux(["set-option", "-guq", name])


def _pane_id(target: str) -> str | None:
    pane_id = _tmux_out(["display-message", "-p", "-t", target, "#{pane_id}"])
    return pane_id or None


def _pane_exists(target: str) -> bool:
    return _pane_id(target) is not None


def _swapped_session() -> str | None:
    session = _tmux_out(["show-options", "-gqv", SWAPPED_SESSION_OPT])
    return session or None


def _orchestrator_pane() -> str | None:
    current_pane = _pane_id(ORCH_TARGET)
    swapped = _swapped_session()
    pane_id = _tmux_out(["show-options", "-gqv", ORCH_PANE_OPT])
    if swapped and pane_id and _pane_exists(pane_id):
        return pane_id
    if swapped and pane_id and not _pane_exists(pane_id):
        _clear_tmux_option(ORCH_PANE_OPT)
        _clear_tmux_option(SWAPPED_SESSION_OPT)
        pane_id = ""
        swapped = None
    if swapped and not pane_id:
        pane_id = _pane_id(f"{swapped}:0.0")
        if pane_id:
            _set_tmux_option(ORCH_PANE_OPT, pane_id)
            return pane_id
        _clear_tmux_option(SWAPPED_SESSION_OPT)

    if current_pane:
        _set_tmux_option(ORCH_PANE_OPT, current_pane)
    return current_pane


def restore_orchestrator() -> bool:
    _close_visible_terminal()
    orchestrator_pane = _orchestrator_pane()
    current_pane = _pane_id(ORCH_TARGET)
    if not orchestrator_pane or not current_pane:
        return False
    if current_pane == orchestrator_pane:
        _clear_tmux_option(SWAPPED_SESSION_OPT)
        return True
    result = _tmux(["swap-pane", "-d", "-s", orchestrator_pane, "-t", ORCH_TARGET])
    if result.returncode != 0:
        return False
    _clear_tmux_option(SWAPPED_SESSION_OPT)
    return True


def switch_to(session: str) -> bool:
    _close_visible_terminal()
    if _swapped_session() == session:
        return restore_orchestrator()

    if not restore_orchestrator():
        return False

    result = _tmux(["swap-pane", "-d", "-s", f"{session}:0.0", "-t", ORCH_TARGET])
    if result.returncode != 0:
        return False
    _set_tmux_option(SWAPPED_SESSION_OPT, session)
    return True


def send_keys(session: str, text: str) -> None:
    restore_orchestrator()
    target = agent_pane_target(session)
    subprocess.run(["tmux", "send-keys", "-t", target, text, ""], capture_output=True)


def send_text_to_pane(target: str, text: str, submit_keys: list[str] | None = None) -> tuple[bool, str]:
    """Paste multiline text into a tmux pane and submit it."""

    submit_keys = submit_keys if submit_keys is not None else ["Enter"]
    buffer_name = f"tmux-dash-heartbeat-{os.getpid()}-{int(time.time() * 1000)}"
    try:
        load = subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=text,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if load.returncode != 0:
            return False, load.stderr.strip() or "tmux load-buffer failed"

        paste = subprocess.run(
            ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", target],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if paste.returncode != 0:
            return False, paste.stderr.strip() or "tmux paste-buffer failed"

        for key in submit_keys:
            if not key:
                continue
            submit = subprocess.run(
                ["tmux", "send-keys", "-t", target, key],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if submit.returncode != 0:
                return False, submit.stderr.strip() or "tmux send-keys failed"
            time.sleep(0.2)
    except Exception as exc:
        return False, str(exc)

    return True, "sent"

# ── AI Summarization ──────────────────────────────────────────────────────────

_pool  = ThreadPoolExecutor(max_workers=4)
_cache: dict[str, tuple[float, str, str]] = {}

_CODEX_NOISE = re.compile(
    r'^(Reading prompt|OpenAI Codex|workdir:|model:|provider:|approval:|sandbox:|'
    r'reasoning|session id:|---+|user$|codex$|tokens used|[\d,]+$)',
    re.MULTILINE,
)
_IDLE_SUMMARY_RE = re.compile(
    r"\b(idle|shell prompt|sitting at a prompt|at a prompt|"
    r"zsh prompt|no active (task|work|goal)|no specific active|"
    r"no visible active|not showing an active|not .*background job|"
    r"does not show .*active|nothing active)\b",
    re.IGNORECASE,
)


def _active_marker_summary(raw: str) -> str:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    markers = [line for line in lines if CODEX_ACTIVE_RE.search(line)]
    marker = markers[-1] if markers else "Codex reports active work or background jobs."
    context = ""
    for line in reversed(lines):
        lowered = line.lower().lstrip("•- ").strip()
        if lowered.startswith(("counts", "status:", "latest status", "latest visible status")):
            context = line
            break
    if context and context != marker:
        return f"Active: {context} {marker}"
    return f"Active: {marker}"


def _guard_active_summary(raw: str, summary: str) -> str:
    if CODEX_ACTIVE_RE.search(raw) and _IDLE_SUMMARY_RE.search(summary):
        return _active_marker_summary(raw)
    return summary


def _summarize_sync(session: str, raw: str) -> str:
    h   = hashlib.md5(raw.encode()).hexdigest()
    now = time.time()
    if session in _cache:
        ts, cached_hash, summary = _cache[session]
        summary = _guard_active_summary(raw, summary)
        if cached_hash == h and (now - ts < SUMMARY_TTL):
            return summary
        if cached_hash != h and (now - ts < SUMMARY_TTL):
            if _cache[session][2] != summary:
                _cache[session] = (now, h, summary)
            return summary
    prompt = (
        "Summarize what this AI coding agent is currently working on "
        "in 1-2 sentences. Be specific about task and status. "
        "If the terminal reports Working, Pursuing goal, or background jobs, "
        "treat the session as active even if a prompt is visible. "
        "Do not describe a session as idle when background jobs are running. "
        "No preamble.\n\n"
        f"<terminal>\n{raw[-2000:]}\n</terminal>"
    )
    try:
        r = subprocess.run(
            ["codex", "exec", "-s", "read-only", "-c", "reasoning_effort=low", prompt],
            capture_output=True, text=True, timeout=60,
        )
        # Response is repeated after "tokens used\n<count>\n"
        m = re.search(r'tokens used\s*\n[\d,]+\s*\n(.*)', r.stdout, re.DOTALL)
        if m:
            summary = m.group(1).strip()
        else:
            # Fallback: strip header noise and take last meaningful line
            clean = _CODEX_NOISE.sub('', r.stdout)
            lines = [l.strip() for l in clean.splitlines() if l.strip()]
            summary = lines[-1] if lines else "·"
    except Exception:
        lines = [l for l in raw.splitlines() if l.strip()]
        summary = "  ".join(lines[-2:]) if lines else "·"
    summary = _guard_active_summary(raw, summary)
    _cache[session] = (now, h, summary)
    return summary


async def summarize(session: str, raw: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, _summarize_sync, session, raw)

# ── Subway Surfers game ───────────────────────────────────────────────────────

GAME_ROWS  = 11   # rows dedicated to subway section
GROUND_ROW =  9   # row index of ground within game section (0-indexed)
CHAR_X     =  8   # fixed character x
JUMP_START_V = 2  # short hop: clears trains without leaving the canvas
JUMP_LEAD_TICKS = 5
JUMP_HANG_TICKS = 5
TRAIN_CLEAR_Y = 4
SLIDE_TICKS = 7
SLIDE_LEAD_TICKS = 4
EFFECT_LIMIT = 18

class SubwayGame:
    THEMES = [
        {
            "name": "TUNNEL",
            "hud": "bold yellow",
            "track": "white",
            "accent": "bold cyan",
            "signs": ["NEXT", "MIND", "PRIME"],
            "light": "·",
            "pillar": "│",
        },
        {
            "name": "STATION",
            "hud": "bold green",
            "track": "bright_white",
            "accent": "bold magenta",
            "signs": ["EXIT", "PLAT", "LOCAL"],
            "light": "•",
            "pillar": "┃",
        },
        {
            "name": "NIGHT",
            "hud": "bold blue",
            "track": "bright_white",
            "accent": "bold yellow",
            "signs": ["MOON", "LOOP", "DASH"],
            "light": "*",
            "pillar": "┆",
        },
        {
            "name": "OUTDOOR",
            "hud": "bold cyan",
            "track": "white",
            "accent": "bold green",
            "signs": ["PARK", "FAST", "CODE"],
            "light": "˙",
            "pillar": "╎",
        },
    ]

    def __init__(self, width: int, billboards: list[str] | None = None):
        self.w = width
        self.billboards = billboards or []
        self.reset()

    # run_frames: 3-frame cycle for legs while on ground
    RUN_FRAMES = [
        [r"\o/", " |\\", "/ | "],   # stride right
        [r"\o/", " | ", "/═\\ "],   # plant
        [r"\o/", r"/| ", " /\\ "],  # stride left
        [r"\o/", " | ", " /\\ "],   # recover
    ]
    # jump_frames keyed by phase: rising / apex / falling
    JUMP_RISE  = [r"\o/",  r" ║ ",  "/ \\ "]
    JUMP_APEX  = [r"\o/",  r"═╪═",  r" _  "]
    JUMP_FALL  = [r"\o/",  r" ╨ ",  "/  \\ "]
    TAKEOFF_FRAME = [r"\o/", "/|\\", "/ \\ "]
    LANDING_FRAME = [r"\o/", r" | ", "/_\\ "]
    # slide (duck under barrier)
    SLIDE_FRAMES = [
        ["    ", r"_o_", r"/_  "],  # tuck
        ["    ", r"-o_", r"__  "],  # flat
    ]
    DEAD_ART   = [r" x_x ", r" )╥( ", r"_/ \_"]
    TRAIN_ART = [
        "╔═══╗",
        "║▣ ▣║",
        "╚═╦═╝",
    ]
    BARRIER_ART = [
        "╤╤╤╤",
        "▓▓▓▓",
    ]
    CHASER_FRAMES = [
        [" o ", "/|\\", "/ \\"],
        [" o ", " |>", "/ \\"],
    ]
    OBSTACLES = {
        "train": {
            "role": "jump",
            "art": TRAIN_ART,
            "base_offset": 3,
            "style": "bold red",
            "clear_y": TRAIN_CLEAR_Y,
        },
        "express": {
            "role": "jump",
            "art": ["╔═══╗", "║▣▣▣║", "╚═╦═╝"],
            "base_offset": 3,
            "style": "bold red",
            "clear_y": TRAIN_CLEAR_Y,
        },
        "signal": {
            "role": "jump",
            "art": [" ◉ ", " ┃ ", " ┻ "],
            "base_offset": 3,
            "style": "bold red",
            "clear_y": 3,
        },
        "barrier": {
            "role": "slide",
            "art": BARRIER_ART,
            "base_offset": 2,
            "style": "bold magenta",
            "clear_y": 1,
        },
        "gate": {
            "role": "slide",
            "art": ["╥╥╥", "███"],
            "base_offset": 2,
            "style": "bold magenta",
            "clear_y": 1,
        },
        "low_barrier": {
            "role": "slide",
            "art": ["▄▄▄▄"],
            "base_offset": 1,
            "style": "bold magenta",
            "clear_y": 1,
        },
    }
    OBSTACLE_WEIGHTS = [
        "train", "train", "express", "barrier", "barrier", "gate", "low_barrier", "signal"
    ]
    POWERUPS = {
        "magnet": {"glyph": "M", "style": "bold cyan", "label": "MAG"},
        "shield": {"glyph": "◈", "style": "bold blue", "label": "SHD"},
        "sneakers": {"glyph": "▵", "style": "bold green", "label": "SNK"},
    }

    def reset(self) -> None:
        self.score      = 0
        self.coins      = 0
        self.ticks      = 0
        self.speed      = 1
        self.char_y     = 0   # cells above ground (0 = on ground)
        self.jump_v     = 0
        self.jump_hang  = 0
        self.sliding    = 0   # ticks remaining in slide
        self.dead       = 0   # ticks remaining in death flash
        self.last_action = "RUN "
        self.action_ticks = 0
        self.streak = 0
        self.combo = 1
        self.shield = False
        self.magnet_ticks = 0
        self.sneakers_ticks = 0
        self.chaser_gap = 7
        self.obstacles: list[dict] = []
        self.coin_items: list[dict] = []
        self.powerups: list[dict] = []
        self.effects: list[dict] = []
        self.spawn_cd   = 18
        self.powerup_cd = 70

    def _theme(self) -> dict:
        return self.THEMES[(self.score // 500) % len(self.THEMES)]

    def _obstacle_spec(self, obstacle: dict) -> dict:
        return self.OBSTACLES[obstacle["type"]]

    def _obstacle_width(self, obstacle: dict) -> int:
        art = self._obstacle_spec(obstacle)["art"]
        return max(len(line) for line in art)

    def _obstacle_role(self, obstacle: dict) -> str:
        return self._obstacle_spec(obstacle)["role"]

    def _obstacle_clear_y(self, obstacle: dict) -> int:
        return self._obstacle_spec(obstacle)["clear_y"]

    def _set_action(self, action: str, ticks: int = 8) -> None:
        self.last_action = action
        self.action_ticks = ticks

    def _add_effect(self, text: str, x: int, y: int, ttl: int = 10,
                    style: str = "bold white", dx: int = -1) -> None:
        self.effects.append({"text": text, "x": x, "y": y, "ttl": ttl, "style": style, "dx": dx})
        if len(self.effects) > EFFECT_LIMIT:
            self.effects = self.effects[-EFFECT_LIMIT:]

    def _jump_obstacle_under_runner(self) -> int:
        runner_left = CHAR_X - 1
        runner_right = CHAR_X + 3
        clear_y = 0
        for obstacle in self.obstacles:
            if self._obstacle_role(obstacle) != "jump":
                continue
            left = obstacle["x"]
            right = left + self._obstacle_width(obstacle) - 1
            if left <= runner_right and right >= runner_left:
                clear_y = max(clear_y, self._obstacle_clear_y(obstacle))
        return clear_y

    def _float_over_visible_jump_obstacle(self) -> None:
        clear_y = self._jump_obstacle_under_runner()
        if not clear_y or self.char_y <= 0:
            return
        self.char_y = max(self.char_y, clear_y)
        self.jump_v = min(self.jump_v, 0)
        self.jump_hang = max(self.jump_hang, 1)

    def _auto_dodge(self) -> None:
        upcoming = [o for o in self.obstacles if o["x"] >= CHAR_X]
        if not upcoming:
            return

        near = min(upcoming, key=lambda o: o["x"])
        dist = near["x"] - CHAR_X
        if dist < -self._obstacle_width(near):
            return

        if self._obstacle_role(near) == "jump":
            lead = self.speed * JUMP_LEAD_TICKS
            can_jump = self.char_y == 0 and self.jump_v == 0
            if can_jump and dist <= lead:
                self.sliding = 0
                if dist - self.speed <= 3:
                    self.jump_v = 0
                    self.char_y = self._obstacle_clear_y(near)
                    self.jump_hang = max(self.jump_hang, 2)
                else:
                    self.jump_v = JUMP_START_V
                    self.char_y = 1
                    bonus_hang = 2 if self.sneakers_ticks > 0 else 0
                    self.jump_hang = max(self.jump_hang, JUMP_HANG_TICKS + bonus_hang)
                self._set_action("HOP ")
        else:
            lead = self.speed * SLIDE_LEAD_TICKS
            can_slide = self.char_y == 0 and self.sliding == 0
            if can_slide and dist <= lead:
                self.sliding = SLIDE_TICKS
                self._set_action("DUCK")

    def tick(self) -> None:
        if self.dead > 0:
            self.dead -= 1
            if self.dead == 0:
                self.reset()
            return

        self.ticks += 1
        self.score += 1
        if self.action_ticks > 0:
            self.action_ticks -= 1
        else:
            self.last_action = "RUN "

        if self.magnet_ticks > 0:
            self.magnet_ticks -= 1
        if self.sneakers_ticks > 0:
            self.sneakers_ticks -= 1
        if self.ticks % 80 == 0:
            self.chaser_gap = max(4, self.chaser_gap - 1)

        # Slide countdown
        if self.sliding > 0:
            self.sliding -= 1

        # Jump physics
        if self.char_y > 0 or self.jump_v > 0:
            clear_y = max(TRAIN_CLEAR_Y, self._jump_obstacle_under_runner())
            if self.jump_hang > 0 and self.jump_v <= 0 and self.char_y >= clear_y:
                self.char_y = clear_y
                self.jump_hang -= 1
            else:
                self.char_y += self.jump_v
                self.jump_v -= 1
                if self.char_y >= clear_y and self.jump_v <= 0 and self.jump_hang > 0:
                    self.char_y = clear_y
                if self.char_y <= 0:
                    self.char_y = 0
                    self.jump_v = 0
                    self.jump_hang = 0

        self._auto_dodge()

        # Move world
        self.obstacles  = [o for o in self.obstacles  if o["x"] > -5]
        self.coin_items = [c for c in self.coin_items if c["x"] > -1]
        self.powerups   = [p for p in self.powerups   if p["x"] > -1]
        for o in self.obstacles:  o["x"] -= self.speed
        for c in self.coin_items: c["x"] -= self.speed
        for p in self.powerups:   p["x"] -= self.speed
        self._pull_magnet_coins()
        self._float_over_visible_jump_obstacle()
        self._tick_effects()

        # Collect coins
        for c in self.coin_items[:]:
            collect_radius = 2 if self.magnet_ticks > 0 else 1
            if abs(c["x"] - CHAR_X) <= collect_radius and abs(c["y"] - self.char_y) <= 1:
                self.coin_items.remove(c)
                self.coins += 1
                self.streak += 1
                self.combo = min(4, 1 + self.streak // 6)
                self.score += self.combo
                self.chaser_gap = min(8, self.chaser_gap + 1)
                self._add_effect(f"+{self.combo}", CHAR_X + 3, GROUND_ROW - max(4, self.char_y + 2), 8, "bold yellow", 0)

        for p in self.powerups[:]:
            if abs(p["x"] - CHAR_X) <= 1 and abs(p["y"] - self.char_y) <= 1:
                self.powerups.remove(p)
                self._collect_powerup(p["type"])

        for o in self.obstacles:
            if not o.get("cleared") and o["x"] + self._obstacle_width(o) < CHAR_X:
                o["cleared"] = True
                bonus = 10 * self.combo
                self.score += bonus
                self.streak += 1
                self.combo = min(4, 1 + self.streak // 6)
                self.chaser_gap = min(8, self.chaser_gap + 1)
                word = "WHOOSH" if self._obstacle_role(o) == "jump" else "CLEAN"
                self._add_effect(f"{word}+{bonus}", CHAR_X + 3, GROUND_ROW - 5, 10, "bold cyan", 0)

        # Spawn
        self.spawn_cd -= 1
        if self.spawn_cd <= 0:
            typ = random.choice(self.OBSTACLE_WEIGHTS)
            self.obstacles.append({"x": self.w - 1, "type": typ})
            self.spawn_cd = random.randint(14, 28)
            self._spawn_coin_pattern(typ)

        self.powerup_cd -= 1
        if self.powerup_cd <= 0:
            self.powerups.append({
                "x": self.w - 2,
                "y": random.choice([2, 3, 4]),
                "type": random.choice(list(self.POWERUPS)),
            })
            self.powerup_cd = random.randint(90, 150)

        if self.ticks % 200 == 0:
            self.speed = min(3, self.speed + 1)

        # Collision → death flash then reset
        for o in self.obstacles:
            if CHAR_X <= o["x"] <= CHAR_X + max(3, self.speed):
                is_sliding = self.sliding > 0
                role = self._obstacle_role(o)
                hit = (role == "jump" and self.char_y < self._obstacle_clear_y(o)) or \
                      (role == "slide" and self.char_y < 1 and not is_sliding)
                if hit:
                    if self.shield:
                        self.shield = False
                        o["cleared"] = True
                        self._add_effect("SHIELD", CHAR_X + 3, GROUND_ROW - 5, 12, "bold blue", 0)
                        continue
                    self.dead = 4
                    self.chaser_gap = max(3, self.chaser_gap - 2)
                    self.streak = 0
                    self.combo = 1
                    self._set_action("HIT ", 4)
                    return

    def _spawn_coin_pattern(self, obstacle_type: str) -> None:
        role = self.OBSTACLES[obstacle_type]["role"]
        if random.random() >= 0.72:
            return
        if role == "jump":
            pattern = [3, 4, 5, 4]
        elif obstacle_type == "low_barrier":
            pattern = [2, 2, 3]
        else:
            pattern = [3, 3, 4]
        for i, y in enumerate(pattern):
            self.coin_items.append({"x": self.w - 5 - i * 3, "y": y})

    def _pull_magnet_coins(self) -> None:
        if self.magnet_ticks <= 0:
            return
        for c in self.coin_items:
            if abs(c["x"] - CHAR_X) > 24:
                continue
            if c["x"] > CHAR_X:
                c["x"] -= min(2, c["x"] - CHAR_X)
            elif c["x"] < CHAR_X:
                c["x"] += 1
            if c["y"] > self.char_y:
                c["y"] -= 1
            elif c["y"] < self.char_y:
                c["y"] += 1

    def _collect_powerup(self, powerup_type: str) -> None:
        if powerup_type == "magnet":
            self.magnet_ticks = 180
        elif powerup_type == "shield":
            self.shield = True
        else:
            self.sneakers_ticks = 180
            self.jump_hang = max(self.jump_hang, JUMP_HANG_TICKS + 2)
        label = self.POWERUPS[powerup_type]["label"]
        self._set_action(label, 16)
        self._add_effect(label, CHAR_X + 3, GROUND_ROW - 6, 16, self.POWERUPS[powerup_type]["style"], 0)

    def _tick_effects(self) -> None:
        kept = []
        for effect in self.effects:
            effect["ttl"] -= 1
            effect["x"] += effect.get("dx", 0)
            if effect["ttl"] > 0:
                kept.append(effect)
        self.effects = kept

    def _put_line(self,
                  buf: list[list[str]],
                  cbuf: list[list[str | None]],
                  y: int,
                  x: int,
                  text: str,
                  style: str | None = None,
                  transparent: bool = True) -> None:
        if not (0 <= y < len(buf)):
            return
        w = len(buf[y])
        for dx, ch in enumerate(text):
            rx = x + dx
            if 0 <= rx < w and (ch != " " or not transparent):
                buf[y][rx] = ch
                cbuf[y][rx] = style if ch != " " else None

    def _draw_hud(self, buf: list[list[str]], cbuf: list[list[str | None]], row0: int) -> None:
        theme = self._theme()
        power = []
        if self.magnet_ticks > 0:
            power.append(f"M{self.magnet_ticks // 10:02d}")
        if self.shield:
            power.append("SHD")
        if self.sneakers_ticks > 0:
            power.append(f"S{self.sneakers_ticks // 10:02d}")
        power_text = " ".join(power) if power else "----"
        state = "HIT " if self.dead > 0 else ("DUCK" if self.sliding > 0 else ("HOP " if self.char_y > 0 else self.last_action))
        bar = (
            f" SUBWAY SURFERS  [{state}]  score {self.score:05d}  coins {self.coins:03d}  "
            f"x{self.combo}  {power_text}  {theme['name']} "
        )
        for i, ch in enumerate(bar[:self.w]):
            if 0 <= row0 < len(buf):
                buf[row0][i] = ch
                cbuf[row0][i] = theme["hud"]

    def _draw_scenery(self, buf: list[list[str]], cbuf: list[list[str | None]], row0: int, ground: int) -> None:
        theme = self._theme()
        w = self.w
        light_row = row0 + 1
        if 0 <= light_row < len(buf):
            for x in range((-self.ticks // 2) % 8, w, 8):
                self._put_line(buf, cbuf, light_row, x, theme["light"], "dim")

        ads = [s[:8].upper() for s in self.billboards if s] or theme["signs"]
        sign_row = row0 + 2
        sign_spacing = 28
        for i in range(max(1, w // sign_spacing + 2)):
            label = ads[(self.ticks // 160 + i) % len(ads)]
            sign = f"╡{label[:8]:^8}╞"
            x = w - ((self.ticks // 2) % sign_spacing) + i * sign_spacing - sign_spacing
            self._put_line(buf, cbuf, sign_row, x, sign, theme["accent"], transparent=False)

        pillar_row = ground - 4
        if 0 <= pillar_row < len(buf):
            for x in range((-self.ticks * self.speed // 2) % 18, w, 18):
                self._put_line(buf, cbuf, pillar_row, x, theme["pillar"], "dim")

    def _draw_tracks(self, buf: list[list[str]], cbuf: list[list[str | None]], ground: int) -> None:
        theme = self._theme()
        w = self.w
        far_rail = ground - 5
        if 0 <= far_rail < len(buf):
            for x in range((-self.ticks) % 13, w, 13):
                self._put_line(buf, cbuf, far_rail, x, "┄┄", "dim")
        mid_rail = ground - 3
        if 0 <= mid_rail < len(buf):
            for x in range((self.ticks // 2) % 10, w, 10):
                self._put_line(buf, cbuf, mid_rail, x, "╼╾", "dim")
        rail = ground - 2
        if 0 <= rail < len(buf):
            for x in range(w):
                buf[rail][x] = "─"
                cbuf[rail][x] = "dim"
        ties = ground - 1
        if 0 <= ties < len(buf):
            for x in range((self.ticks * self.speed) % 6, w, 6):
                self._put_line(buf, cbuf, ties, x, "╱", "dim")
        if 0 <= ground < len(buf):
            for x in range(w):
                buf[ground][x] = "═"
                cbuf[ground][x] = theme["track"]

    def _draw_collectibles(self, buf: list[list[str]], cbuf: list[list[str | None]], ground: int) -> None:
        coin_frames = ["○", "◌", "●", "◌"]
        coin_glyph = coin_frames[(self.ticks // 2) % len(coin_frames)]
        for c in self.coin_items:
            self._put_line(buf, cbuf, ground - c["y"], c["x"], coin_glyph, "bold yellow")
        for p in self.powerups:
            spec = self.POWERUPS[p["type"]]
            self._put_line(buf, cbuf, ground - p["y"], p["x"], spec["glyph"], spec["style"])

    def _draw_obstacles(self, buf: list[list[str]], cbuf: list[list[str | None]], ground: int) -> None:
        for o in self.obstacles:
            spec = self._obstacle_spec(o)
            base = ground - spec["base_offset"]
            for dy, line in enumerate(spec["art"]):
                self._put_line(buf, cbuf, base + dy, o["x"], line, spec["style"], transparent=False)

    def _draw_effects(self, buf: list[list[str]], cbuf: list[list[str | None]], row0: int) -> None:
        for effect in self.effects:
            style = effect["style"]
            if effect["ttl"] <= 3:
                style = "dim"
            self._put_line(buf, cbuf, row0 + effect["y"], effect["x"], effect["text"], style)

    def _draw_chaser(self, buf: list[list[str]], cbuf: list[list[str | None]], ground: int) -> None:
        x = max(0, CHAR_X - self.chaser_gap - 2)
        art = self.CHASER_FRAMES[(self.ticks // 3) % len(self.CHASER_FRAMES)]
        for dy, line in enumerate(art):
            self._put_line(buf, cbuf, ground - 2 + dy, x, line, "dim red")

    def _runner_art(self) -> tuple[list[str], str]:
        if self.dead > 0:
            return self.DEAD_ART, "bold red"
        if self.char_y > 0:
            if self.jump_v > 1:
                return self.TAKEOFF_FRAME, "bold cyan"
            if self.jump_v >= 0 or self.jump_hang > 0:
                return self.JUMP_APEX, "bold cyan"
            if self.char_y <= 1:
                return self.LANDING_FRAME, "bold cyan"
            return self.JUMP_FALL, "bold cyan"
        if self.sliding > 0:
            return self.SLIDE_FRAMES[self.ticks % len(self.SLIDE_FRAMES)], "bold yellow"
        return self.RUN_FRAMES[self.ticks % len(self.RUN_FRAMES)], "bold green"

    def _draw_runner(self, buf: list[list[str]], cbuf: list[list[str | None]], ground: int) -> None:
        if self.char_y > 0:
            shadow = "▁▁▁" if self.char_y < 3 else "▁▁"
            self._put_line(buf, cbuf, ground - 1, CHAR_X - 1, shadow, "dim")
            trail = "≈" if self.ticks % 2 == 0 else "≋"
            self._put_line(buf, cbuf, ground - max(2, self.char_y), CHAR_X - 4, trail, "dim cyan")
        elif self.sliding > 0:
            self._put_line(buf, cbuf, ground - 1, CHAR_X - 4, "—", "dim yellow")
        elif self.dead == 0:
            dust = "·" if self.ticks % 2 == 0 else "˙"
            self._put_line(buf, cbuf, ground - 1, CHAR_X - 4, dust, "dim")

        char_art, char_col = self._runner_art()
        feet_row = ground - self.char_y
        for dy, line in enumerate(char_art):
            self._put_line(buf, cbuf, feet_row - 2 + dy, CHAR_X - 1, line, char_col)

    def render_buf(self,
                   buf:  list[list[str]],
                   cbuf: list[list[str | None]],
                   row0: int) -> None:
        ground = row0 + GROUND_ROW
        self._draw_hud(buf, cbuf, row0)
        self._draw_scenery(buf, cbuf, row0, ground)
        self._draw_tracks(buf, cbuf, ground)
        self._draw_collectibles(buf, cbuf, ground)
        self._draw_obstacles(buf, cbuf, ground)
        self._draw_effects(buf, cbuf, row0)
        self._draw_chaser(buf, cbuf, ground)
        self._draw_runner(buf, cbuf, ground)


# ── Combined screensaver canvas ───────────────────────────────────────────────

class ScreenSaverCanvas(Widget):
    """Top half: auto-playing subway surfers. Bottom half: animated session view."""

    BOTTOM_MODES = ("BOUNCE", "CARAMELLDANSEN", "MATRIX", "EQUALIZER", "STARFIELD")
    MODE_TICKS = 180

    def __init__(self, sessions: list[str], **kwargs):
        super().__init__(**kwargs)
        self._sessions = sessions
        self.game: SubwayGame | None = None
        self.balls: list[dict] = []
        self.frames = 0

    def _init_balls(self, w: int, bounce_h: int) -> None:
        self.balls = []
        for i, s in enumerate(self._sessions):
            vx = random.choice([-1, 1]) * random.choice([1, 2])
            vy = random.choice([-1, 1])
            self.balls.append({
                "session": s,
                "idx": i + 1,
                "color": CARD_COLORS[i % len(CARD_COLORS)],
                "summary": _cache[s][2] if s in _cache else "·",
                "x": random.randint(0, max(1, w - CARD_W)),
                "y": random.randint(0, max(1, bounce_h - CARD_H)),
                "vx": vx,
                "vy": vy,
            })

    def on_mount(self) -> None:
        self.set_interval(1 / 10, self._tick)

    def _tick(self) -> None:
        self.frames += 1
        w = self.size.width or 80
        h = self.size.height or 40
        bounce_h = max(CARD_H + 1, h - GAME_ROWS - 1)

        if self.game is None:
            self.game = SubwayGame(w, self._sessions)
        if not self.balls:
            self._init_balls(w, bounce_h)

        self.game.w = w
        self.game.billboards = self._sessions
        self.game.tick()

        # Card motion is reserved for BOUNCE so the other modes can own the motion.
        mode = self._bottom_mode()
        balls = self.balls
        if mode == "BOUNCE":
            for b in balls:
                b["x"] += b["vx"]; b["y"] += b["vy"]
                if b["x"] < 0:
                    b["x"] = 0;              b["vx"] =  abs(b["vx"])
                elif b["x"] + CARD_W > w:
                    b["x"] = w - CARD_W;     b["vx"] = -abs(b["vx"])
                if b["y"] < 0:
                    b["y"] = 0;              b["vy"] =  abs(b["vy"])
                elif b["y"] + CARD_H > bounce_h:
                    b["y"] = bounce_h - CARD_H; b["vy"] = -abs(b["vy"])

            for i in range(len(balls)):
                for j in range(i + 1, len(balls)):
                    a, b = balls[i], balls[j]
                    ox = CARD_W - abs(a["x"] - b["x"])
                    oy = CARD_H - abs(a["y"] - b["y"])
                    if ox > 0 and oy > 0:
                        if ox <= oy:
                            if a["x"] < b["x"]: a["x"] = b["x"] - CARD_W
                            else:               b["x"] = a["x"] - CARD_W
                            a["vx"], b["vx"] = b["vx"], a["vx"]
                        else:
                            if a["y"] < b["y"]: a["y"] = b["y"] - CARD_H
                            else:               b["y"] = a["y"] - CARD_H
                            a["vy"], b["vy"] = b["vy"], a["vy"]

        max_x = max(0, w - CARD_W)
        max_y = max(0, bounce_h - CARD_H)
        for b in balls:
            b["x"] = min(max(0, b["x"]), max_x)
            b["y"] = min(max(0, b["y"]), max_y)

        self.refresh()

    def _bottom_mode(self) -> str:
        return self.BOTTOM_MODES[(self.frames // self.MODE_TICKS) % len(self.BOTTOM_MODES)]

    def _card_position(self,
                       index: int,
                       ball: dict,
                       mode: str,
                       w: int,
                       bounce_h: int) -> tuple[int, int]:
        if mode == "BOUNCE":
            return ball["x"], ball["y"]

        gap_x = 2
        gap_y = 1
        columns = max(1, w // (CARD_W + gap_x))
        columns = min(columns, max(1, len(self.balls)))
        rows = (len(self.balls) + columns - 1) // columns
        total_w = columns * CARD_W + (columns - 1) * gap_x
        total_h = rows * CARD_H + (rows - 1) * gap_y
        start_x = max(0, (w - total_w) // 2)
        start_y = max(0, bounce_h - total_h)
        row = index // columns
        col = index % columns
        return start_x + col * (CARD_W + gap_x), start_y + row * (CARD_H + gap_y)

    def _put_bg(self,
                buf: list[list[str]],
                cbuf: list[list[str | None]],
                y: int,
                x: int,
                text: str,
                style: str | Style | None = "dim") -> None:
        if not (0 <= y < len(buf)):
            return
        w = len(buf[y])
        for dx, ch in enumerate(text):
            rx = x + dx
            if 0 <= rx < w and ch != " ":
                buf[y][rx] = ch
                cbuf[y][rx] = style

    def _draw_bottom_background(self,
                                buf: list[list[str]],
                                cbuf: list[list[str | None]],
                                y0: int,
                                height: int,
                                mode: str) -> None:
        if height <= 0:
            return
        w = len(buf[0]) if buf else 0
        if w <= 0:
            return

        self._put_bg(buf, cbuf, y0, 1, f" {mode.lower()} ", "dim")
        if mode == "BOUNCE":
            for y in range(height):
                x = (self.frames + y * 7) % max(1, w)
                self._put_bg(buf, cbuf, y0 + y, x, "◇", "dim cyan")
            for x in range((self.frames // 2) % 12, w, 12):
                self._put_bg(buf, cbuf, y0 + height - 1, x, "╱", "dim")
            return

        if mode == "CARAMELLDANSEN":
            frames = _get_caramelldansen_frames()
            if not frames:
                self._put_bg(buf, cbuf, y0 + height // 2, 2, "missing caramelldansen asset", "bold red")
                return

            frame = frames[(self.frames // CARAMELLDANSEN_FRAME_TICKS) % len(frames)]
            art = _fit_ascii_frame(frame, w, height)
            art_w = max((len(line) for line in art), default=0)
            art_h = len(art)
            x0 = max(0, (w - art_w) // 2)
            y = y0 + max(0, (height - art_h) // 2)
            for dy, line in enumerate(art):
                self._put_bg(buf, cbuf, y + dy, x0, line, CARAMELLDANSEN_STYLE)

            beat = (self.frames // 4) % 2
            wave = "♪  CARAMELLDANSEN  ♫  " if beat == 0 else "♫  ASCII DANCE LOOP  ♪  "
            wave_y = y0 + height - 1
            for x in range((-self.frames) % len(wave) - len(wave), w, len(wave)):
                self._put_bg(buf, cbuf, wave_y, x, wave, CARAMELLDANSEN_WAVE_STYLE)
            return

        if mode == "MATRIX":
            glyphs = "01{}[]<>/\\"
            for x in range(0, w, 3):
                head = (self.frames + x * 5) % max(1, height + 8)
                for tail in range(7):
                    y = head - tail
                    if 0 <= y < height:
                        glyph = glyphs[(x + y + self.frames // 3) % len(glyphs)]
                        style = "bold green" if tail == 0 else "dim green"
                        self._put_bg(buf, cbuf, y0 + y, x, glyph, style)
            return

        if mode == "EQUALIZER":
            levels = "▁▂▃▄▅▆▇█"
            base = y0 + height - 1
            max_bar = max(2, min(10, height - 2))
            for x in range(0, w, 3):
                bar_h = 1 + ((x * 3 + self.frames * 2 + (x // 3) ** 2) % max_bar)
                glyph = levels[(bar_h + self.frames // 2) % len(levels)]
                for y in range(bar_h):
                    style = "bold cyan" if y == bar_h - 1 else "dim blue"
                    self._put_bg(buf, cbuf, base - y, x, glyph, style)
            for x in range((-self.frames // 2) % 18, w, 18):
                self._put_bg(buf, cbuf, y0 + 1, x, "BEAT", "dim cyan")
            return

        # STARFIELD
        stars = [".", "·", "*", "+", "✦"]
        for y in range(height):
            for x in range(((y * 11 - self.frames) % 17), w, 17):
                depth = (x + y * 3 + self.frames // 2) % len(stars)
                style = "bold white" if depth >= 3 else "dim"
                self._put_bg(buf, cbuf, y0 + y, x, stars[depth], style)
        tunnel_y = y0 + height // 2
        for offset in range(0, min(w // 2, height * 2), 4):
            left = max(0, w // 2 - offset - (self.frames % 4))
            right = min(w - 1, w // 2 + offset + (self.frames % 4))
            row = tunnel_y + (offset // 4) % max(1, height // 2)
            self._put_bg(buf, cbuf, row, left, "\\", "dim white")
            self._put_bg(buf, cbuf, row, right, "/", "dim white")

    def render(self) -> Text:
        w = self.size.width or 80
        h = self.size.height or 40
        inner    = CARD_W - 2
        bounce_y = GAME_ROWS + 1   # row where bounce section starts
        bounce_h = h - bounce_y

        buf:  list[list[str]]           = [[" "] * w for _ in range(h)]
        cbuf: list[list[str | None]]    = [[None]    * w for _ in range(h)]

        # Subway game (top)
        if self.game:
            self.game.render_buf(buf, cbuf, 0)

        # Divider
        div = GAME_ROWS
        if 0 <= div < h:
            for x in range(w):
                buf[div][x] = "─"
                cbuf[div][x] = "dim"

        mode = self._bottom_mode()
        self._draw_bottom_background(buf, cbuf, bounce_y, bounce_h, mode)

        # Bouncing cards (bottom)
        for i, ball in enumerate(self.balls):
            rel_x, rel_y = self._card_position(i, ball, mode, w, bounce_h)
            cx  = rel_x
            cy  = rel_y + bounce_y
            col = ball["color"]
            name    = ball["session"][:inner - 5]
            summary = ball["summary"][:inner - 2]
            rows = [
                "╭" + "─" * inner + "╮",
                f"│ [{ball['idx']}] {name:<{inner-5}}│",
                f"│ {summary:<{inner-2}} │",
                "│" + " " * inner + "│",
                "│" + " " * inner + "│",
                "╰" + "─" * inner + "╯",
            ]
            for dy, row_str in enumerate(rows):
                ry = cy + dy
                if 0 <= ry < h:
                    for dx, ch in enumerate(row_str):
                        rx = cx + dx
                        if 0 <= rx < w:
                            buf[ry][rx] = ch
                            cbuf[ry][rx] = col

        text = Text()
        for ry in range(h):
            for rx in range(w):
                ch  = buf[ry][rx]
                col = cbuf[ry][rx]
                text.append(ch, style=col) if col else text.append(ch)
            text.append("\n")
        return text


class BounceScreen(Screen):
    BINDINGS = [("escape", "dismiss", "Back")]

    def __init__(self, sessions: list[str], **kwargs):
        super().__init__(**kwargs)
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        yield ScreenSaverCanvas(self._sessions)
        yield Static(" [dim]any key to exit[/dim]", id="bounce-hint")

    def on_key(self, _: events.Key) -> None:
        if self.app.screen is self:
            self.dismiss()

    DEFAULT_CSS = """
    #bounce-hint { dock: bottom; height: 1; padding: 0 1; background: $surface; }
    ScreenSaverCanvas { height: 1fr; width: 1fr; overflow: hidden hidden; }
    """

# ── Dashboard widgets ─────────────────────────────────────────────────────────

CONTROLS = (
    " [cyan][0][/cyan] restore orch  "
    "[cyan][1-9][/cyan] swap  "
    "[cyan][t+#][/cyan] terminal  "
    "[cyan][m+#][/cyan] message  "
    "[cyan][h][/cyan] heartbeat  "
    "[cyan][b][/cyan] bounce  "
    "[cyan][r][/cyan] refresh  "
    "[cyan][q][/cyan] quit"
)

STATUS_STYLES = {
    "active": "bold green",
    "idle": "bold yellow",
    "waiting": "bold magenta",
    "blocked": "bold red",
    "error": "bold red reverse",
    "new": "dim",
}


class SessionCard(Static):
    summary: reactive[str] = reactive("…")
    status: reactive[str] = reactive("new")
    status_reason: reactive[str] = reactive("waiting for first capture")
    idle_seconds: reactive[float] = reactive(0.0)

    DEFAULT_CSS = """
    SessionCard {
        border: round $primary;
        padding: 0 1;
        height: 8;
    }
    SessionCard.sub {
        border: round $warning;
    }
    """

    def __init__(self, session: str, idx: int, monitor: SessionMonitor, **kwargs):
        super().__init__(**kwargs)
        self.session_name = session
        self.idx = idx
        self.monitor = monitor
        self._last_raw = ""
        self._last_summary_at = 0.0
        if session in SUBSESSIONS:
            self.add_class("sub")

    def render(self) -> str:
        parent = SUBSESSIONS.get(self.session_name)
        tag = f" [dim](↳ {parent})[/dim]" if parent else ""
        label = SESSION_LABELS.get(self.session_name, self.session_name)
        if label != self.session_name:
            tag = f" [dim]({self.session_name})[/dim]{tag}"
        status_style = STATUS_STYLES.get(self.status, "dim")
        badge = f"[{status_style}]{self.status.upper()}[/]"
        idle = format_duration(self.idle_seconds)
        return (
            f"[bold cyan][{self.idx}][/bold cyan] "
            f"[bold]{label}[/bold]{tag} {badge} [dim]{idle}[/dim]\n"
            f"[dim]{self.summary}[/dim]\n"
            f"[dim]{self.status_reason}[/dim]"
        )

    def watch_summary(self, _: str) -> None:
        self.refresh()

    def watch_status(self, _: str) -> None:
        self.refresh()

    def watch_status_reason(self, _: str) -> None:
        self.refresh()

    def watch_idle_seconds(self, _: float) -> None:
        self.refresh()

    async def refresh_summary(self) -> None:
        raw = capture(self.session_name, 50)
        snapshot = self.monitor.observe(self.session_name, raw)
        self.status = snapshot.status
        self.status_reason = snapshot.reason
        self.idle_seconds = snapshot.idle_seconds
        now = time.time()
        if raw == self._last_raw and now - self._last_summary_at < SUMMARY_TTL:
            return
        self._last_raw = raw
        self.summary = await summarize(self.session_name, raw)
        self._last_summary_at = time.time()


class MsgModal(ModalScreen):
    DEFAULT_CSS = """
    MsgModal { align: center middle; }
    #box { width: 60; height: 9; border: thick $accent; padding: 1 2; background: $surface; }
    """

    def __init__(self, session: str, **kwargs):
        super().__init__(**kwargs)
        self.target = session

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"[bold cyan]→ {self.target}[/bold cyan]")
            yield Input(placeholder="message to send…")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if event.value.strip():
            send_keys(self.target, event.value)
        self.dismiss()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss()

# ── App ───────────────────────────────────────────────────────────────────────

class Dash(App):
    TITLE = "tmux-dash"
    CSS = """
    #controls { height: 1; padding: 0 1; background: $primary-darken-3; dock: top; }
    Grid { grid-size: 2; grid-gutter: 1 1; padding: 1 1; }
    """
    BINDINGS = [("h", "heartbeat", "Heartbeat"), ("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sessions: list[str] = []
        self._pending_msg  = False
        self._pending_term = False
        self._monitor = SessionMonitor(STATUS_CONFIG)
        self._orch_monitor = SessionMonitor(ORCH_MONITOR_CONFIG)

    def compose(self) -> ComposeResult:
        self._sessions = get_sessions()
        yield Static(CONTROLS, id="controls")
        yield Grid(*(SessionCard(s, i + 1, self._monitor) for i, s in enumerate(self._sessions)))
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(REFRESH_SECS, self.action_refresh)
        if HEARTBEAT_ENABLED:
            self.set_interval(HEARTBEAT_SECS, self._scheduled_heartbeat)
        for card in self.query(SessionCard):
            self.run_worker(card.refresh_summary)

    async def action_refresh(self) -> None:
        if HEARTBEAT_ENABLED:
            await asyncio.to_thread(self._observe_orchestrator)
        sessions = get_sessions()
        if sessions != self._sessions:
            self._sessions = sessions
            grid = self.query_one(Grid)
            grid.query("SessionCard").remove()
            for i, s in enumerate(sessions):
                card = SessionCard(s, i + 1, self._monitor)
                await grid.mount(card)
                self.run_worker(card.refresh_summary)
        else:
            for card in self.query(SessionCard):
                self.run_worker(card.refresh_summary)

    def _scheduled_heartbeat(self) -> None:
        self.run_worker(self._run_heartbeat(force=False), group="heartbeat", exclusive=True)

    def action_heartbeat(self) -> None:
        self.run_worker(self._run_heartbeat(force=True), group="heartbeat", exclusive=True)

    def _collect_snapshots(self) -> list[SessionSnapshot]:
        snapshots: list[SessionSnapshot] = []
        for session in get_sessions():
            raw = capture(session, 80)
            snapshots.append(self._monitor.observe(session, raw))
        return snapshots

    def _observe_orchestrator(self) -> SessionSnapshot:
        raw = capture(ORCH_TARGET, 80)
        return self._orch_monitor.observe("orchestrator", raw)

    def _orchestrator_is_quiet(self) -> tuple[bool, str]:
        had_history = "orchestrator" in self._orch_monitor.histories
        snapshot = self._observe_orchestrator()
        if snapshot.status == "error":
            return False, snapshot.reason
        if not had_history:
            return True, "orchestrator observed for the first time"
        quiet_after = ORCH_MONITOR_CONFIG.idle_after_secs
        if snapshot.changed:
            return False, "orchestrator pane changed on this check"
        if snapshot.idle_seconds < quiet_after:
            return False, f"orchestrator pane changed {format_duration(snapshot.idle_seconds)} ago"
        return True, f"orchestrator quiet for {format_duration(snapshot.idle_seconds)}"

    def _cached_summaries(self, snapshots: list[SessionSnapshot]) -> dict[str, str]:
        return {
            snapshot.session: _cache[snapshot.session][2]
            for snapshot in snapshots
            if snapshot.session in _cache
        }

    async def _run_heartbeat(self, force: bool) -> None:
        if not HEARTBEAT_ENABLED and not force:
            return

        snapshots = await asyncio.to_thread(self._collect_snapshots)
        prompt = build_heartbeat_prompt(
            snapshots=snapshots,
            summaries=self._cached_summaries(snapshots),
            labels=SESSION_LABELS,
            orch_target=ORCH_TARGET,
        )
        event: dict[str, Any] = {
            "event": "heartbeat",
            "mode": "manual" if force else "scheduled",
            "target": ORCH_TARGET,
            "timestamp": time.time(),
            "snapshots": [snapshot_to_dict(snapshot) for snapshot in snapshots],
        }

        if not force:
            quiet, reason = await asyncio.to_thread(self._orchestrator_is_quiet)
            if not quiet:
                event.update({"injected": False, "reason": reason})
                await asyncio.to_thread(append_ledger, HEARTBEAT_LEDGER_PATH, event)
                self.notify(f"Heartbeat skipped: {reason}", timeout=4)
                return

        sent, detail = await asyncio.to_thread(send_text_to_pane, ORCH_TARGET, prompt, HEARTBEAT_SUBMIT_KEYS)
        event.update({"injected": sent, "reason": detail})
        await asyncio.to_thread(append_ledger, HEARTBEAT_LEDGER_PATH, event)
        if sent:
            self.notify(f"Heartbeat sent to {ORCH_TARGET}", timeout=3)
        else:
            self.notify(f"Heartbeat failed: {detail}", severity="error", timeout=5)

    def _digit(self, n: int) -> None:
        idx = n - 1
        if not (0 <= idx < len(self._sessions)):
            return
        session = self._sessions[idx]
        if self._pending_msg:
            self._pending_msg = False
            self.push_screen(MsgModal(session))
        elif self._pending_term:
            self._pending_term = False
            restore_orchestrator()
            opened, detail = open_terminal(session)
            if opened:
                self.notify(f"Opened terminal in {session}: {detail}", timeout=3)
            else:
                self.notify(f"Could not open terminal in {session}: {detail}", severity="error", timeout=5)
        else:
            if not switch_to(session):
                self.notify(f"Could not switch to {session}", severity="error", timeout=3)

    def key_0(self) -> None:
        if restore_orchestrator():
            self.notify("Orchestrator restored", timeout=2)
        else:
            self.notify("Could not restore orchestrator", severity="error", timeout=3)

    def key_1(self): self._digit(1)
    def key_2(self): self._digit(2)
    def key_3(self): self._digit(3)
    def key_4(self): self._digit(4)
    def key_5(self): self._digit(5)
    def key_6(self): self._digit(6)
    def key_7(self): self._digit(7)
    def key_8(self): self._digit(8)
    def key_9(self): self._digit(9)

    def key_m(self) -> None:
        self._pending_msg  = False
        self._pending_term = False
        self._pending_msg  = True
        self.notify("Press session number to message…", timeout=2)

    def key_t(self) -> None:
        self._pending_msg  = False
        self._pending_term = True
        self.notify("Press session number to open terminal…", timeout=2)

    def key_b(self) -> None:
        self.push_screen(BounceScreen(self._sessions))


def main() -> None:
    Dash().run()


if __name__ == "__main__":
    main()
