from pathlib import Path

from tmux_dash import restore_codex_orch as restore


def test_codex_resume_command_quotes_reasoning_effort() -> None:
    command = restore._codex_resume_command("session-id", "gpt-5.5", "xhigh")

    assert command == "codex resume -m gpt-5.5 -c reasoning_effort=xhigh --yolo session-id"


def test_dashboard_command_keeps_pane_open_and_logs_exit() -> None:
    command = restore._dashboard_command(Path("/tmp/tmux dash/exits.log"))

    assert "tmux-dash; code=$?" in command
    assert "tee -a '/tmp/tmux dash/exits.log'" in command
    assert 'exec "$shell" -l' in command


def test_find_pane_by_resume_id(monkeypatch) -> None:
    monkeypatch.setattr(
        restore,
        "_tmux_out",
        lambda _args: "%1 codex resume old\n%2 codex resume 019e4735-8270-7eb2-9f88-7cabcd53c4f7\n",
    )

    assert restore._find_pane_by_resume_id("019e4735-8270-7eb2-9f88-7cabcd53c4f7") == "%2"
