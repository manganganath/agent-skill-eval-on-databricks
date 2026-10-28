"""Agent skill registry and evaluation for Databricks."""

__version__ = "0.1.0"


class SkillHubError(Exception):
    """An expected, user-facing failure. The CLI prints it and exits 1."""
