"""Web interface and FastAPI application."""

from cognition.web.web import app, should_tick_immediately

__all__ = ["app", "should_tick_immediately"]
