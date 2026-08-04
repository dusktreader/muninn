import tomllib
from importlib import metadata


def get_version_from_metadata() -> str:
    """Read the installed package version."""
    return metadata.version(__package__ or __name__)


def get_version_from_pyproject() -> str:
    """Read the development version from pyproject.toml."""
    with open("pyproject.toml", "rb") as file:
        return tomllib.load(file)["project"]["version"]


def get_version() -> str:
    """Resolve the package version in installed and source checkouts."""
    try:
        return get_version_from_metadata()
    except metadata.PackageNotFoundError:
        try:
            return get_version_from_pyproject()
        except (FileNotFoundError, KeyError):
            return "unknown"
