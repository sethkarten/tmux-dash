# tmux-dash System Design

`tmux-dash` is a terminal control surface for a multi-agent tmux workspace. It
does not replace the orchestrator agent. Its job is to keep the user and the
orchestrator informed, make pane control fast, and provide enough state to spot
idle or blocked sessions.

## Current Architecture

```mermaid
flowchart LR
    user[User] --> tui[tmux-dash TUI]

    tui --> cards[Live session cards]
    tui --> swap[Pane swap controls]
    tui --> msg[Manual message modal]
    tui --> term[Side terminal launcher]
    tui --> saver[Screensaver]
    wall[tmux-eva wallboard] --> resolver

    cards --> resolver[Codex pane resolver]
    resolver --> capture[tmux capture-pane]
    cards --> summaries[Optional Codex summaries]
    summaries --> codex[codex CLI]

    swap --> tmux[tmux server]
    msg --> tmux
    term --> tmux
    capture --> tmux

    tmux --> orch[orch:0.0 orchestrator pane]
    tmux --> agents[Agent sessions]

    saver --> subway[Autoplay subway runner]
    saver --> ascii[Light-blue ASCII Caramelldansen]
    saver --> modes[Matrix, equalizer, starfield, bounce]
```

## Feature Map

| Feature | Status | Owner |
| --- | --- | --- |
| Live cards for tmux sessions | Current | `tmux-dash` |
| Optional AI summaries from recent pane output | Current | `tmux-dash` + `codex` CLI |
| Swap a session into the orchestrator pane | Current | `tmux-dash` |
| Restore the orchestrator pane | Current | `tmux-dash` |
| Manual message sending to a session | Current | `tmux-dash` |
| Side terminal split below the displayed Codex pane | Current | `tmux-dash` |
| Animated screensaver with visible session cards | Current | `tmux-dash` |
| Configurable session order, exclusions, subsessions, and orchestrator target | Current | `tmux-dash` |
| Idle, blocked, waiting, and error detection | Current | `tmux-dash` |
| Status badges on session cards | Current | `tmux-dash` |
| JSONL progress ledger | Current | `tmux-dash` |
| 10-minute orchestrator heartbeat prompt | Current | `tmux-dash` scheduler + orchestrator Codex |
| Read-only second-monitor wallboard | Current | `tmux-eva` |

## Heartbeat Loop

The heartbeat loop should keep all orchestration decisions centered in the
orchestrator pane. `tmux-dash` collects context and injects a status prompt. The
orchestrator Codex summarizes, asks the user for approval, and sends approved
messages to the other sessions directly.

```mermaid
sequenceDiagram
    participant Timer as tmux-dash timer
    participant Tmux as tmux
    participant Detector as State detector
    participant Ledger as JSONL ledger
    participant Orch as orch:0.0 Codex
    participant User
    participant Agents as Agent sessions

    loop Every 10 minutes
        Timer->>Tmux: capture recent output from non-orch sessions
        Timer->>Detector: classify active, idle, waiting, blocked, error
        Detector->>Ledger: append snapshot
        Timer->>Orch: inject heartbeat prompt with summaries and state
        Orch->>User: print progress update and recommended next actions
        User->>Orch: approve or edit proposed messages
        Orch->>Agents: send approved instructions directly
        Orch->>Ledger: optional note via visible pane output
    end
```

## Heartbeat Responsibilities

`tmux-dash` should:

- Capture recent output from configured non-orchestrator sessions.
- Resolve agent sessions to their main Codex pane (`session:0.0`) rather than
  tmux's active pane, so side terminals do not drive status labels.
- Detect coarse state such as `active`, `idle`, `waiting`, `blocked`, and
  `error`.
- Treat Codex `Working`, `Pursuing goal`, and background-job markers as active
  work even when the visible pane text is stable.
- Record heartbeat snapshots to a local JSONL progress ledger.
- Inject a concise status prompt into `orch:0.0` on a configurable interval.
- Avoid injecting while the orchestrator pane appears busy, unless manually
  triggered.

The orchestrator Codex should:

- Summarize current progress across sessions in its own pane.
- Recommend next actions for the user to approve.
- After approval, send messages directly to the target tmux sessions.
- Preserve user-defined operational constraints.

## Configuration Shape

```toml
[orchestrator]
enabled = true
target = "orch:0.0"
heartbeat_secs = 600
idle_after_secs = 900
quiet_secs = 20
submit_keys = ["Tab", "Enter"]
ledger_path = "~/.local/state/tmux-dash/orchestrator.jsonl"

[status]
detect_tracebacks = true
detect_repeated_output = true
detect_waiting_prompts = true

[sessions.programBench]
label = "ProgramBench eval"

[sessions.balrogRL]
label = "BALROG RL"
```

Session metadata is optional and should describe subsessions or agent sessions
only. The orchestrator remains the place where goals, approvals, and decisions
are discussed.

## Second-Monitor Wallboard

`tmux-eva` is a separate read-only Textual app for a dedicated monitor. It uses
the same config, session discovery, Codex-pane resolver, and status detector as
`tmux-dash`, but it does not expose any controls that mutate tmux sessions. It
uses an original black/red/orange/green command-system theme and renders:

- Global phase: `NOMINAL`, `WATCH`, or `ALERT`.
- Operation mode: `NORMAL OPERATION`, `OBSERVATION WATCH`,
  `COMMAND INTERRUPT`, or `FAULT CASCADE`.
- Counts for active, idle, waiting, blocked, and error sessions.
- Unit rows for each session with sync-style meters, power state, and
  authorization/border state.
- Diagnostic bus rows under the unit matrix so the wallboard stays visually
  dense even when there are only a few active sessions.
- A focused unit diagnostic panel with a derived signal waveform, recent
  terminal signal, active Codex markers, link-lattice rows, and auxiliary
  background-job count.
- Limiter checks, register-noise rows, and border-field maps that are generated
  from session digests to keep tall monitor layouts filled.
- A tri-core decision panel that votes from logic, ops, and link perspectives.
- A command-protocol countdown based on the heartbeat cadence.
- A command log derived from current session status and auxiliary job state.
- A repeating protocol tape for dense right-rail telemetry.
- An alert cascade for non-active sessions and active background-job markers.

The focused unit rotates every 15 seconds, while `n` can still advance it
manually and `p` can pause the wallboard.
