"""Отображение входных путей на выходные.

Воспроизводит семантику `Tools/scripts/filter.js:84-131` и исправляет три её
дефекта:

* `getFileName` проверял только `FileExists`, поэтому два входных файла,
  отображающиеся в одно назначение **в рамках одного прогона**, оба получали
  имя без суффикса, и второй молча затирал первый. Здесь ведётся множество
  зарезервированных назначений.
* при более 9999 совпадениях функция возвращала пустую строку, путь уходил в
  stderr и попадал в корзину «Images with characters» — неверное сообщение в
  неверной корзине.
* поиск свободного индекса был O(n²) по числу совпадений в каталоге.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path, PurePath
from typing import Dict, Optional, Tuple

#: Регистронезависимые файловые системы: Windows и, по умолчанию, macOS.
CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"

#: До этого значения индекс дополняется нулями до четырёх знаков — так делал
#: `filter.js`, и такие имена уже лежат у пользователей на диске.
PADDED_LIMIT = 9999
#: Дальше суффикс продолжается без выравнивания, чтобы не терять файлы.
HARD_LIMIT = 1_000_000


class CannotNameOutput(Exception):
    """Свободное имя в каталоге назначения подобрать не удалось."""


def normkey(path) -> str:
    """Ключ сравнения путей.

    На Windows и macOS файловая система нечувствительна к регистру, на Linux —
    чувствительна. Ошибка в эту сторону означала бы, что `Logo.png` и `logo.png`
    в одном каталоге либо ошибочно считаются одним файлом, либо ошибочно
    затирают друг друга.
    """
    text = os.path.normpath(str(path))
    return os.path.normcase(text) if CASE_INSENSITIVE else text


def long_path(path: Path) -> str:
    """Добавить префикс `\\\\?\\` длинным путям на Windows.

    Безопасно именно благодаря решению D1: сами инструменты видят только
    короткий путь во временном каталоге, а с длинными работает лишь Python.
    """
    text = str(path)
    if os.name != "nt" or len(text) < 240:
        return text
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _split_name(name: str) -> Tuple[str, str]:
    stem = PurePath(name).stem
    suffix = PurePath(name).suffix
    return stem, suffix


class OutputMapper:
    """Считает, куда положить результат для каждого входного файла."""

    def __init__(self, outdir: Optional[Path]):
        #: None означает «перезаписать оригиналы» (в 2.7 — пустой `%outdir%`).
        self.outdir = Path(outdir).resolve() if outdir is not None else None
        self._reserved = set()
        self._next_index: Dict[Tuple[str, str, str], int] = {}
        self._roots: Dict[str, Path] = {}
        # Рекурсивный замок: `destination` вызывает и `prepare_root`, и
        # `_free_name`. Без него параллельные потоки ломают ровно то, ради чего
        # существует множество зарезервированных имён: два потока одновременно
        # не находят корень в `_roots`, оба заводят выходной подкаталог, и файлы
        # одного дерева расползаются по `Фото` и `Фото-0001`. Хуже того, двум
        # потокам может достаться одно и то же имя файла.
        self._lock = threading.RLock()

    # -- служебное ---------------------------------------------------------

    @property
    def in_place(self) -> bool:
        return self.outdir is None

    def _taken(self, path: Path) -> bool:
        return normkey(path) in self._reserved or os.path.lexists(long_path(path))

    def _reserve(self, path: Path) -> Path:
        self._reserved.add(normkey(path))
        return path

    def _free_name(self, target: Path) -> Path:
        """Подобрать свободное имя, при необходимости добавив `-0001`."""
        if not self._taken(target):
            return self._reserve(target)
        stem, suffix = _split_name(target.name)
        cache_key = (normkey(target.parent), stem, suffix)
        index = self._next_index.get(cache_key, 1)
        while index < HARD_LIMIT:
            tag = "%04d" % index if index <= PADDED_LIMIT else str(index)
            candidate = target.parent / ("%s-%s%s" % (stem, tag, suffix))
            index += 1
            if not self._taken(candidate):
                self._next_index[cache_key] = index
                return self._reserve(candidate)
        self._next_index[cache_key] = index
        raise CannotNameOutput(
            "в каталоге %s не удалось подобрать свободное имя для %s"
            % (target.parent, target.name)
        )

    def _free_dir(self, target: Path) -> Path:
        if not os.path.isdir(long_path(target)) and normkey(target) not in self._reserved:
            return self._reserve(target)
        stem, suffix = _split_name(target.name)
        index = 1
        while index < HARD_LIMIT:
            tag = "%04d" % index if index <= PADDED_LIMIT else str(index)
            candidate = target.parent / ("%s-%s%s" % (stem, tag, suffix))
            index += 1
            if not os.path.isdir(long_path(candidate)) and \
                    normkey(candidate) not in self._reserved:
                return self._reserve(candidate)
        raise CannotNameOutput(
            "в каталоге %s не удалось подобрать свободное имя для подкаталога %s"
            % (target.parent, target.name)
        )

    # -- публичный интерфейс ----------------------------------------------

    def prepare_root(self, root: Path) -> None:
        """Зафиксировать выходной подкаталог для входного корня.

        Считается один раз на корень, поэтому две папки `Photos`, брошенные в
        одну и ту же цель, дают `Photos` и `Photos-0001`, а не смешиваются.
        """
        if self.in_place:
            return
        with self._lock:
            key = normkey(root)
            if key in self._roots:
                return
            if root.is_dir():
                self._roots[key] = self._free_dir(self.outdir / root.name)
            else:
                self._roots[key] = self.outdir

    def destination(self, src: Path, root: Path) -> Path:
        """Куда записать результат для `src`, пришедшего из корня `root`."""
        if self.in_place:
            return src
        # Как и в filter.js:91 — если каталог назначения совпадает с каталогом
        # самого файла, для этого файла работаем на месте. Проверка именно
        # пофайловая, а не глобальная.
        if normkey(self.outdir) == normkey(src.parent):
            return src
        with self._lock:
            self.prepare_root(root)
            out_root = self._roots[normkey(root)]
            if root.is_dir():
                try:
                    relative = src.relative_to(root)
                except ValueError:
                    relative = Path(src.name)
                target = out_root / relative
            else:
                target = out_root / src.name
            return self._free_name(target)
