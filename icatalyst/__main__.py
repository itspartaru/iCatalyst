"""Точка входа: `python3 -m icatalyst` и консольный скрипт `icatalyst`."""

from __future__ import annotations

import sys

from .cli import run


def main() -> int:
    try:
        return run(sys.argv[1:])
    except KeyboardInterrupt:
        # Тот же текст, что печатает cli.run, когда прерывание перехвачено
        # внутри цикла обработки. Сообщений о прерывании ровно два места, и
        # разными они быть не должны: консольный интерфейс англоязычный, как в
        # 2.7, а тест не обязан угадывать, какая из ветвей сработала.
        sys.stderr.write("\n Interrupted by user.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
