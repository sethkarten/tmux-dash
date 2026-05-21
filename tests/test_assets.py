from tmux_dash.app import (
    CARAMELLDANSEN_ASSET,
    _fit_ascii_frame,
    _load_ascii_frames,
)


def test_caramelldansen_asset_loads() -> None:
    frames = _load_ascii_frames(CARAMELLDANSEN_ASSET)

    assert len(frames) == 45
    assert all(any(line.strip() for line in frame) for frame in frames)


def test_ascii_frame_fit_bounds_output() -> None:
    frames = _load_ascii_frames(CARAMELLDANSEN_ASSET)
    fitted = _fit_ascii_frame(frames[0], max_w=80, max_h=24)

    assert fitted
    assert len(fitted) <= 24
    assert max(len(line) for line in fitted) <= 80
