"""Point d'entrée : `python3 -m agentos` lance le démon."""

import sys

from .daemon import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
