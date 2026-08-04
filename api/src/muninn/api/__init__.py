"""Expose the FastAPI application without importing it for submodules."""


def __getattr__(name: str):
    """Load the application lazily so database modules remain independently importable."""
    if name == "app":
        from muninn.api.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["app"]
