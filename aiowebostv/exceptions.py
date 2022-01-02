"""Exceptions for aiowebostv."""


class WebOsTvPairException(Exception):
    """Exception raised to represent TV pairing errors."""


class WebOsTvCmdException(Exception):
    """Exception raised to represent TV command exceptions."""


class WebOsTvCmdError(WebOsTvCmdException):
    """Exception raised to represent TV command errors."""


class WebOsTvServiceNotFoundError(WebOsTvCmdError):
    """Exception raised to represent TV service not found error."""
