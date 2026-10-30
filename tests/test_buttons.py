"""Tests for aiowebostv buttons."""

from aiowebostv.buttons import BUTTONS


def test_buttons() -> None:
    """Test the known buttons are unique upper case names."""
    assert len(BUTTONS) == len(set(BUTTONS))
    assert all(button == button.upper() for button in BUTTONS)
