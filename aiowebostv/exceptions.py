"""Exceptions for aiowebostv."""


class WebOsTvError(Exception):
    """Base exception for aiowebostv."""


class WebOsTvConnectionError(WebOsTvError):
    """Represent a failure to connect to the TV, other than pairing."""


class WebOsTvRegistrationError(WebOsTvConnectionError):
    """Represent a transient registration error on an already paired TV."""


class WebOsTvPairError(WebOsTvError):
    """Represent TV pairing errors."""


class WebOsTvCommandError(WebOsTvError):
    """Represent TV command errors."""


class WebOsTvCommandTimeoutError(WebOsTvCommandError):
    """Represent TV command timeout error."""


class WebOsTvResponseTypeError(WebOsTvCommandError):
    """Represent TV responded with error type."""


class WebOsTvServiceNotFoundError(WebOsTvResponseTypeError):
    """Represent TV service not found error."""


class WebOsTvWakeError(WebOsTvError):
    """Represent an error waking the TV with Wake-on-LAN."""
