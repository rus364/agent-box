"""Pin direct helper launches to this package, even in a same-named workspace."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_box.cli import main  # noqa: E402

main()
