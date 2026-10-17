"""Convenience root wrapper for scripts/add_forwarder.py."""
import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if __name__ == "__main__":
    runpy.run_path(str(_ROOT / "scripts" / "add_forwarder.py"), run_name="__main__")
