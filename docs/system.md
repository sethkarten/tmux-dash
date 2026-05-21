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

    cards --> capture[tmux capture-pane]
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
| Side terminal for a session | Current | `tmux-dash` |
| Animated screensaver with visible session cards | Current | `tmux-dash` |
| Configurable session order, exclusions, subsessions, and orchestrator target | Current | `tmux-dash` |
| Idle, blocked, waiting, and error detection | Planned | `tmux-dash` |
| Status badges on session cards | Planned | `tmux-dash` |
| JSONL progress ledger | Planned | `tmux-dash` |
| 10-minute orchestrator heartbeat prompt | Planned | `tmux-dash` scheduler + orchestrator Codex |

## Planned Heartbeat Loop

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
- Detect coarse state such as `active`, `idle`, `waiting`, `blocked`, and
  `error`.
- Record heartbeat snapshots to a local JSONL progress ledger.
- Inject a concise status prompt into `orch:0.0` on a configurable interval.
- Avoid injecting while the orchestrator pane appears busy, unless manually
  triggered.

The orchestrator Codex should:

- Summarize current progress across sessions in its own pane.
- Recommend next actions for the user to approve.
- After approval, send messages directly to the target tmux sessions.
- Preserve user-defined operational constraints.

## Planned Configuration Shape

```toml
[orchestrator]
enabled = true
target = "orch:0.0"
heartbeat_secs = 600
idle_after_secs = 900
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
