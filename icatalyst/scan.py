"""Обход входных данных.

Заменяет связку `:makefilelist` → `dir /a-d /b /s` → `filter.js` из 2.7 — и
вместе с ней исчезает главная причина исходного бага: вывод `dir`
перекодировался в cp866, и всё, чего в этой кодировке нет, превращалось в `?`
или похожий символ (`iCatalyst.bat:303`). `os.scandir` отдаёт имена в Unicode,
терять там нечего.

Порядок обхода отсортирован, а не «как отдала файловая система»: это делает
вывод детерминированным и, значит, проверяемым эталонными файлами.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

#: Расширение → формат. Сравнение регистронезависимое: в корпусе есть `Ёлка.PNG`.
EXTENSIONS: Dict[str, str] = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".jpe": "jpg",
    ".gif": "gif",
}


@dataclass
class ScanResult:
    #: (файл, корень, формат) в порядке обхода.
    files: List[Tuple[Path, Path, str]] = field(default_factory=list)
    #: Корни в порядке, в котором их передал пользователь.
    roots: List[Path] = field(default_factory=list)
    #: Пути, которых не существует.
    missing: List[str] = field(default_factory=list)
    #: Форматы, реально найденные во входных данных. Меню показываются только
    #: для них — так же, как в 2.7.
    present: Set[str] = field(default_factory=set)

    def count(self, fmt: str) -> int:
        return sum(1 for _, _, f in self.files if f == fmt)


def classify(name: str) -> Optional[str]:
    return EXTENSIONS.get(os.path.splitext(name)[1].lower())


def _walk(root: Path) -> List[Tuple[Path, str]]:
    """Рекурсивно собрать подходящие файлы, отсортированно и без переходов по симлинкам."""
    found: List[Tuple[Path, str]] = []
    stack = [root]
    seen: Set[str] = set()
    while stack:
        directory = stack.pop()
        try:
            real = os.path.realpath(directory)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            fmt = classify(entry.name)
            if fmt is not None:
                found.append((Path(entry.path), fmt))
        # Обратный порядок, потому что stack — LIFO: так обход остаётся
        # алфавитным сверху вниз.
        stack.extend(reversed(subdirs))
    return found


def scan(inputs: Sequence[str]) -> ScanResult:
    result = ScanResult()
    seen: Set[str] = set()
    for raw in inputs:
        path = Path(os.path.abspath(os.path.expanduser(str(raw))))
        key = os.path.normcase(str(path)) if os.name == "nt" else str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            result.roots.append(path)
            for file_path, fmt in _walk(path):
                result.files.append((file_path, path, fmt))
                result.present.add(fmt)
        elif path.is_file():
            fmt = classify(path.name)
            if fmt is None:
                # Не изображение по расширению — в 2.7 такой файл просто
                # игнорировался при перечислении.
                result.missing.append(str(path))
                continue
            result.roots.append(path)
            result.files.append((path, path, fmt))
            result.present.add(fmt)
        else:
            result.missing.append(str(path))
    return result
