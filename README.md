# tmux-dash

A terminal dashboard for monitoring and steering multiple AI-agent tmux sessions.

`tmux-dash` shows live session cards with short AI summaries, lets you swap an
agent pane into an orchestrator pane, send a message to a session, open a side
terminal, launch an animated screensaver with status cards still visible, and
send periodic heartbeat prompts to the orchestrator pane.

## Requirements

- Python 3.12+
- tmux
- `uv` or another Python package installer
- Optional: `codex` CLI for AI summaries. Without it, `tmux-dash` falls back to
  recent terminal lines.

## Install

From a local checkout:

```bash
uv tool install .
```

From GitHub:

```bash
uv tool install git+https://github.com/sethkarten/tmux-dash
```

For editable development:

```bash
uv sync --dev
uv run tmux-dash
```

## Configure

By default, `tmux-dash` lists all tmux sessions except `orch`, sorted
alphabetically. To control ordering and the orchestrator pane, copy the example:

```bash
mkdir -p ~/.config/tmux-dash
cp examples/config.toml ~/.config/tmux-dash/config.toml
```

Useful config fields:

```toml
agent_order = ["programBench", "emulatorBench", "FLE"]
exclude = ["orch"]
orch_target = "orch:0.0"

[orchestrator]
enabled = true
target = "orch:0.0"
heartbeat_secs = 600
submit_key = "Tab"
ledger_path = "~/.local/state/tmux-dash/orchestrator.jsonl"

[subsessions]
gameboy-dmg-codex = "emulatorBench"
```

You can also point at a one-off config:

```bash
TMUX_DASH_CONFIG=/path/to/config.toml tmux-dash
```

## Controls

- `0`: restore the orchestrator pane
- `1`-`9`: swap a session into the orchestrator pane
- `t` then `1`-`9`: open a side terminal for that session
- `m` then `1`-`9`: send a message to that session
- `h`: send a heartbeat prompt to the orchestrator pane now
- `b`: open the screensaver
- `r`: refresh
- `q`: quit

## Orchestrator Heartbeat

When `[orchestrator].enabled` is true, `tmux-dash` captures the recent output
from non-orchestrator sessions every `heartbeat_secs`, classifies each session
as `active`, `idle`, `waiting`, `blocked`, or `error`, writes a JSONL snapshot,
and injects a prompt into the orchestrator pane.

Codex treats multiline pasted prompts as queued paste content, so the default
heartbeat submit key is `Tab`. Set `submit_key = "Enter"` if your orchestrator
pane is a plain shell or another tool that submits on Enter.

The orchestrator Codex is responsible for summarizing progress, recommending
next actions, asking the user for approval, and sending approved messages to
other sessions directly. `tmux-dash` only supplies context and timing.

## Screensaver

The screensaver keeps session cards visible while rotating background modes,
including an autoplay subway runner, bouncing cards, matrix rain, equalizer,
starfield, and a bundled ASCII Caramelldansen animation.

## System Design

See [docs/system.md](docs/system.md) for the architecture diagram, current
feature map, and orchestrator heartbeat loop.
