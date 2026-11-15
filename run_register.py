"""Run the package directly from an extracted ZIP, without installing it."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from frontier_register.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
