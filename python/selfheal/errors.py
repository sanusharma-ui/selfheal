"""Public SDK configuration errors; messages must not include secret values."""


class ConfigurationError(ValueError):
    """The developer must supply valid local configuration before healing."""
