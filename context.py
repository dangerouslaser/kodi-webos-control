"""Context menu entry point: "Play with Filmmaker Mode" and friends."""

import sys

from resources.lib.context import main

if __name__ == "__main__":
    main(sys.argv, getattr(sys, "listitem", None))
