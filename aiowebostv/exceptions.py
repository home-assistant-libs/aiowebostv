"""Exceptions for aiowebostv."""


class PyLGTVPairException(Exception):
    """Exception raised to represent TV pairing errors."""


class PyLGTVCmdException(Exception):
    """Exception raised to represent TV command exceptions."""


class PyLGTVCmdError(PyLGTVCmdException):
    """Exception raised to represent TV command errors."""


class PyLGTVServiceNotFoundError(PyLGTVCmdError):
    """Exception raised to represent TV service not found error."""
