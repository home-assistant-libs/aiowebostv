"""Tests for aiowebostv models."""

from aiowebostv.models import WebOsTvInfo, WebOsTvState


def test_tv_info_clear() -> None:
    """Test clearing TV info resets all fields."""
    info = WebOsTvInfo(
        hello={"a": 1}, system={"b": 2}, software={"c": 3}, connection={"d": 4}
    )

    info.clear()

    assert info == WebOsTvInfo()


def test_tv_state_clear() -> None:
    """Test clearing TV state resets all fields."""
    state = WebOsTvState(
        power_state={"state": "Active"},
        current_app_id="netflix",
        sound_output="tv_speaker",
        muted=True,
        volume=10,
        apps={"netflix": {}},
        inputs={"hdmi1": {}},
        media_state=[{"playState": "playing"}],
        current_channel={"channelId": "1"},
        channel_info={"channel": {}},
        channels=[{"channelId": "1"}],
        is_on=True,
        is_screen_on=True,
    )

    state.clear()

    assert state == WebOsTvState()
