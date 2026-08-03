#!/usr/bin/env python3
"""Запуск из распакованной папки, без установки пакета.

Нужен ровно потому, что `python -m icatalyst` требует, чтобы корень репозитория
был в `sys.path`, а пользователь, бросивший папку на ярлык, ничего про это знать
не должен.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from icatalyst.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
