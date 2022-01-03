"""Exceptions for aiowebostv."""


class WebOsTvError(Exception):
    """Base exception for aiowebostv."""


class WebOsTvPairError(WebOsTvError):
    """Represent TV pairing errors."""


class WebOsTvCommandError(WebOsTvError):
    """Represent TV command errors."""


class WebOsTvResponseTypeError(WebOsTvCommandError):
    """Represent TV responded with error type."""


class WebOsTvServiceNotFoundError(WebOsTvResponseTypeError):
    """Represent TV service not found error."""
