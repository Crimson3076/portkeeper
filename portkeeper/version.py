import os

from . import __version__


def version_info():
    """Running version, with the build commit when the image provides one."""
    version = os.environ.get("PORTKEEPER_VERSION", "").strip().removeprefix("v") or __version__
    commit = os.environ.get("PORTKEEPER_COMMIT", "").strip()
    return {"version": version, "commit": commit[:40] or None}
