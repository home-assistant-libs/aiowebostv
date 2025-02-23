"""LG webOS TV models."""

from dataclasses import MISSING, dataclass, field, fields
from typing import Any

WebOsTvStateValue = dict[str, Any] | str | bool | int | list[dict[str, Any]] | None


@dataclass
class WebOsTvInfo:
    """Represent LG webOS TV info."""

    hello: dict[str, Any] = field(default_factory=dict[str, Any])
    system: dict[str, Any] = field(default_factory=dict[str, Any])
    software: dict[str, Any] = field(default_factory=dict[str, Any])

    def clear(self) -> None:
        """Reset all fields to their default values."""
        for f in fields(self):
            if f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())


@dataclass
class WebOsTvState:
    """Represent the state of a LG webOS TV."""

    power_state: dict[str, Any] = field(default_factory=dict[str, Any])
    current_app_id: str | None = None
    sound_output: str | None = None
    muted: bool | None = None
    volume: int | None = None
    apps: dict[str, Any] = field(default_factory=dict[str, Any])
    inputs: dict[str, Any] = field(default_factory=dict[str, Any])
    media_state: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # Can't be empty dict, None is used to check if we need to subscribe to updates
    current_channel: dict[str, Any] | None = None
    channel_info: dict[str, Any] | None = None
    channels: list[dict[str, Any]] | None = None
    # Calculated fields
    is_on: bool = False
    is_screen_on: bool = False

    def clear(self) -> None:
        """Reset all fields to their default values."""
        for f in fields(self):
            # Reset to default value or default factory
            if f.default is not MISSING:
                setattr(self, f.name, f.default)
            elif f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())
            else:
                setattr(self, f.name, None)
