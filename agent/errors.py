"""Shared agent exceptions."""


class AuthRequired(Exception):
    """YouTube credentials are missing a needed scope or have been revoked."""


class PublishError(Exception):
    """A publish attempt failed (never reported as successful unless verified)."""


class ResearchError(Exception):
    """All research sources failed."""
