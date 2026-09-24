class ProviderError(RuntimeError):
    """A safe-to-display provider failure (never contains response bodies)."""


class ProviderUnavailable(ProviderError):
    """Transport, authentication, quota or service failure; fallback is allowed."""


class InvalidProposal(ProviderError):
    """Untrusted or incomplete model output; fail closed without fallback."""


class ContextRejected(ProviderError):
    """Context exceeds a limit or cannot be shared safely."""
