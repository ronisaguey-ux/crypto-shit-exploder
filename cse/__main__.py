"""Entry point so the documented `python -m cse <command>` works."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
