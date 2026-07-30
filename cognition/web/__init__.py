"""Web interface and FastAPI application."""

from cognition.web.web import app, webhook_signal

__all__ = ["app", "webhook_signal"]
