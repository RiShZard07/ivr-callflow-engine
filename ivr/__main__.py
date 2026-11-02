"""Allow ``python -m ivr``."""

import sys

from ivr.cli import main

if __name__ == "__main__":
    sys.exit(main())
