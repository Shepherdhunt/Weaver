class WeaverError(Exception):
    """A user-facing error: reported without a traceback by the CLI."""


class ConfigError(WeaverError):
    pass


class StaleEvidenceError(WeaverError):
    """Evidence or a validation result no longer matches the source it describes."""
