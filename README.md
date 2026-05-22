# tmux-dash

A terminal dashboard for monitoring and steering multiple AI-agent tmux sessions.

`tmux-dash` shows live session cards with short AI summaries, lets you swap an
agent pane into an orchestrator pane, send a message to a session, open a side
terminal, launch an animated screensaver with status cards still visible, and
send periodic heartbeat prompts to the orchestrator pane.

It also ships `tmux-eva`, a separate read-only wallboard for a second monitor.
`tmux-eva` uses the same session detection but presents it as a dense
black/red/orange/green command-system display with unit diagnostics, alert
cascades, sync meters, and command-protocol timing.

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
submit_keys = ["Tab", "Enter"]
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
- `t` then `1`-`9`: open a side terminal below that agent's Codex pane
- `m` then `1`-`9`: send a message to that session
- `h`: send a heartbeat prompt to the orchestrator pane now
- `b`: open the screensaver
- `r`: refresh
- `q`: quit

## Second-Monitor Wallboard

Run `tmux-eva` for a separate, read-only monitor view:

```bash
tmux-eva
```

The wallboard shows global phase, operation mode, unit cards, sync-style
meters, auxiliary/background-job power state, a focused unit diagnostic pane,
diagnostic bus rows, tri-core decision votes, a link-lattice readout, signal
waveform, recent terminal signal, active Codex markers, command-protocol
countdown, command log, protocol tape, border-field maps, limiter checks, and
alert cascade. It also animates fluctuating sync, signal packets, a dense
border-field scanner, pilot-sync scope traces, glyph buses, and corner timers.
It intentionally fills the whole screen with redundant telemetry so it reads
like an overloaded control-room monitor instead of a sparse status board.
It never swaps panes or sends messages. Controls are `r` refresh, `n` next
target, `p` pause, and `q` quit. The focused unit rotates automatically every
15 seconds unless you pause the wallboard.

## Orchestrator Heartbeat

When `[orchestrator].enabled` is true, `tmux-dash` captures the recent output
from non-orchestrator sessions every `heartbeat_secs`, classifies each session
as `active`, `idle`, `waiting`, `blocked`, or `error`, writes a JSONL snapshot,
and injects a prompt into the orchestrator pane.

Session cards, heartbeat snapshots, and manual messages target each agent's
main Codex pane (`session:0.0`) instead of tmux's currently active pane. If an
agent is swapped into the orchestrator view, `tmux-dash` reads that displayed
pane instead. This keeps side terminals from making a session look idle.

Codex activity markers such as `Working`, `Pursuing goal`, and background-job
footers keep a session marked `active` even when the visible pane text has not
changed recently.

Codex treats multiline pasted prompts as queued paste content, so the default
heartbeat submit sequence is `Tab` then `Enter`. Set `submit_keys = ["Enter"]`
if your orchestrator pane is a plain shell or another tool that submits on
Enter.

The orchestrator Codex is responsible for summarizing progress, recommending
next actions, asking the user for approval, and sending approved messages to
other sessions directly. `tmux-dash` only supplies context and timing.

## Side Terminals

Press `t`, then a session number. `tmux-dash` swaps that agent's Codex pane into
the orchestrator view and creates a vertical terminal split directly below it,
using the agent pane's current working directory. The dashboard stays visible in
`orch`; focus moves to the new terminal pane so you can type immediately.

## Screensaver

The screensaver keeps session cards visible while rotating background modes,
including an autoplay subway runner, bouncing cards, matrix rain, equalizer,
starfield, and a bundled ASCII Caramelldansen animation.

## System Design

See [docs/system.md](docs/system.md) for the architecture diagram, current
feature map, and orchestrator heartbeat loop.
