"""Apply a new package once on its next host launch; running sessions keep their runtime."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from . import config, migrations, runtime_bundle
from .errors import AgentBoxError
from .paths import APPLIED_PACKAGE, PACKAGE_APPLY_LOCK, PACKAGE_ROOT


@contextmanager
def apply_lock():
    PACKAGE_APPLY_LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(PACKAGE_APPLY_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def in_session():
    return bool(os.environ.get("AGENT_BOX_RUNTIME_CONFIG")) or Path("/run/.containerenv").exists()


def reconcile(*, force=False):
    """A failed apply leaves the old marker, so retry never depends on a manual reset.

    A content identity also covers editable installations. Session snapshots
    never trigger host application: their older runtime and partial settings
    must not downgrade integrations or turn off omitted host settings.
    """
    if not force and (in_session() or not config.CONFIG_FILE.is_file()):
        return []
    from . import runtime
    try:
        with apply_lock():
            identity = runtime_bundle.identity(PACKAGE_ROOT)
            try:
                previous = migrations.read(APPLIED_PACKAGE, migrations.APPLIED_PACKAGE)
            except FileNotFoundError:
                previous = {}
            if not force and previous.get("package") == identity:
                return []
            config.migrate_config()
            saved = config.load_config()
            report = runtime.apply_config(saved)
            migrations.write(APPLIED_PACKAGE, {"package": identity}, migrations.APPLIED_PACKAGE)
            return (["applied updated agent-box package"] if not force else []) + report
    except (OSError, ValueError) as exc:
        detail = (f": {exc.strerror}" if isinstance(exc, OSError) and exc.strerror
                  else ": invalid stored JSON")
        raise AgentBoxError("Cannot apply the installed agent-box package" + detail
                            + "; correct the stored file or access problem and retry the launch "
                            "or run agent-box config apply") from exc


def before_launch():
    import sys
    for line in reconcile():
        print(f"agent-box: {line}", file=sys.stderr)


def repository_guidance(settings, root):
    """Repositories adopt package guidance lazily, including those opened after global apply.

    The block hash is its revision: another per-package registry would repeat
    that fact and miss projects restored from older branches. Rendered sessions
    must not replace newer host guidance with their immutable older package.
    """
    if not settings.workflow.enabled or in_session():
        return None
    from .repo_binding import Binding, refresh_guidance
    try:
        if Binding.session(root) is None:
            return None
        return refresh_guidance(root)
    except (AgentBoxError, OSError, UnicodeError) as exc:
        return {"status": "warning", "path": str(root / "AGENTS.md"),
                "message": f"Workflow guidance was preserved: {exc}. Reconcile the file and run ab-setup; "
                           "do not overwrite user edits or assume the managed instructions are current."}


def forget():
    """Reinstalling the same package after uninstall must restore its integrations."""
    with apply_lock():
        APPLIED_PACKAGE.unlink(missing_ok=True)
