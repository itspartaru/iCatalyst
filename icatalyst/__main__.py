"""Точка входа: `python3 -m icatalyst` и консольный скрипт `icatalyst`."""

from __future__ import annotations

import sys

from .cli import run


def main() -> int:
    try:
        return run(sys.argv[1:])
    except KeyboardInterrupt:
        sys.stderr.write("\nПрервано пользователем.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
