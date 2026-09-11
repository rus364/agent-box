"""Consume one launch's private environment inside an existing tmux server."""
import json
import os
from pathlib import Path
import sys


def main():
    path = Path(sys.argv[1])
    try:
        environment = json.loads(path.read_text())
    finally:
        path.unlink(missing_ok=True)
    # These identify the new pane, not the caller's possibly nested tmux.
    for name in ("TERM", "TMUX", "TMUX_PANE"):
        if name in os.environ:
            environment[name] = os.environ[name]
    os.execvpe(sys.argv[2], sys.argv[2:], environment)


if __name__ == "__main__":
    main()
