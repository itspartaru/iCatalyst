"""Поиск внешних инструментов, их опрос и матрица возможностей.

Одна и та же таблица зондов используется приложением и скриптом сборки, чтобы
«скрипт говорит, что собралось» и «программа говорит, что работает» не могли
разъехаться.

Ключевая деталь: поиск идёт через переопределяемый список каталогов, поэтому
тесты подсовывают поддельные оптимизаторы через `ICATALYST_TOOLS_DIR` и
проверяют весь конвейер на машине, где не установлено ни одного настоящего.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import app_directory

#: Флаги, скрывающие мелькающее окно консоли на Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: Окружение для зондов: сообщения инструментов должны быть на английском,
#: иначе разбор их вывода зависит от локали пользователя.
_C_ENV = {"LC_ALL": "C", "LANG": "C"}


class ToolMissing(Exception):
    """Инструмент не найден. Сообщение содержит, что именно установить."""

    def __init__(self, name: str, searched: Sequence[str], apt: str = "",
                 build: str = "", fallback: str = ""):
        self.name = name
        self.searched = list(searched)
        self.apt = apt
        self.build = build
        self.fallback = fallback
        lines = ["инструмент %s не найден" % name]
        if apt:
            lines.append("  установить: sudo apt install %s" % apt)
        if build:
            lines.append("  или собрать: python3 Tools/build_tools.py --build --only %s" % build)
        if fallback:
            lines.append("  без него: %s" % fallback)
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    #: Имена исполняемого файла, по которым его ищут в PATH, в порядке
    #: предпочтения. Позволяет найти jpegtran из mozjpeg раньше системного.
    aliases: Tuple[str, ...]
    #: Аргументы зонда. Код возврата игнорируется: многие из этих утилит
    #: печатают справку и выходят с ненулевым кодом.
    probe: Tuple[str, ...] = ()
    #: Подстрока, обязательная в выводе зонда.
    accept: str = ""
    #: Регулярное выражение для извлечения версии.
    version_re: str = ""
    #: Возможность → подстрока, доказывающая её наличие.
    caps: Dict[str, str] = field(default_factory=dict)
    #: Отдельный вызов для определения возможностей, если зонд их не показывает.
    caps_args: Tuple[str, ...] = ()
    #: Не запускать вовсе. Для закрытых утилит, чьё поведение без аргументов
    #: не документировано и может оказаться интерактивным (тогда зонд повиснет
    #: на живом канале).
    never_execute: bool = False
    apt: str = ""
    build: str = ""
    fallback: str = ""
    platforms: Tuple[str, ...] = ()


TOOL_SPECS: Dict[str, ToolSpec] = {
    "optipng": ToolSpec(
        name="optipng", aliases=("optipng",),
        # Зонд именно `-v`: вывод `-h` начинается со слова «Synopsis» и названия
        # программы не содержит вообще, так что опознать её по справке нельзя.
        probe=("-v",), accept="OptiPNG",
        version_re=r"OptiPNG version ([0-9.]+)",
        caps={"zlib-params": "-zc", "strip": "-strip"},
        caps_args=("-h",),
        apt="optipng",
        fallback="структурная оптимизация PNG будет пропущена",
    ),
    "oxipng": ToolSpec(
        name="oxipng", aliases=("oxipng",), probe=("--version",), accept="oxipng",
        version_re=r"oxipng ([0-9.]+)",
        caps={"zopfli": "--zopfli", "alpha": "--alpha", "strip": "--strip"},
        caps_args=("--help",),
        build="oxipng",
        fallback="вместо него будет использован optipng",
    ),
    "zopflipng": ToolSpec(
        name="zopflipng", aliases=("zopflipng",), probe=(), accept="zopflipng",
        caps={"filters": "--filters", "iterations": "--iterations"},
        apt="zopfli",
        fallback="режим Xtreme деградирует до Advanced",
    ),
    "advdef": ToolSpec(
        name="advdef", aliases=("advdef",), probe=("--version",),
        # Программа представляется названием пакета, а не своим: «advancecomp
        # vnone by Andrea Mazzoleni». Версия — `\S+`, а не число, потому что
        # Debian собирает пакет без версии и печатает буквально «vnone».
        accept="advancecomp",
        version_re=r"advancecomp v(\S+)",
        # Уровни сжатия: 1=zlib, 2=libdeflate, 3=7z, 4=zopfli. То есть `-2` —
        # это libdeflate, а не 7-Zip, и системный advancecomp 2.x даёт то же,
        # что вложенный в Windows advdef 2.0.
        caps={"libdeflate": "shrink-normal", "zopfli": "shrink-insane"},
        caps_args=("--help",),
        apt="advancecomp",
        fallback="дополнительное пересжатие deflate будет пропущено",
    ),
    "gifsicle": ToolSpec(
        name="gifsicle", aliases=("gifsicle",), probe=("--version",), accept="Gifsicle",
        version_re=r"Gifsicle ([0-9.]+)",
        apt="gifsicle",
    ),
    "jpegtran": ToolSpec(
        name="jpegtran",
        aliases=("jpegtran-mozjpeg", "mozjpeg-jpegtran", "jpegtran"),
        probe=("-h",), accept="-optimize",
        # `-revert` есть только в MozJPEG и означает «выключить нестандартные
        # дефолты MozJPEG». Отсюда следует, что baseline-режим воспроизводит и
        # обычный libjpeg-turbo, а MozJPEG нужен лишь для progressive.
        caps={"mozjpeg": "-revert"},
        caps_args=("-version",),
        version_re=r"((?:libjpeg-turbo|mozjpeg|jpegtran) version \S+)",
        apt="libjpeg-turbo-progs",
        build="mozjpeg",
    ),
    "pngwolf": ToolSpec(
        name="pngwolf",
        aliases=("pngwolf", "pngwolf-zopfli", "pngwolfzopfli"),
        probe=("--help",), accept="--in=",
        # v1.1.x заменил плоские флаги на подопции: --out-deflate=zopfli,iter=15
        # вместо --zopfli-iter=15. Определяем, какая форма перед нами.
        caps={"new-cli": "--out-deflate", "legacy-cli": "--zopfli-iter"},
        build="pngwolf",
        fallback="режим Xtreme будет использовать zopfli через oxipng",
    ),
    "exiftool": ToolSpec(
        name="exiftool", aliases=("exiftool",), probe=("-ver",), accept=".",
        version_re=r"([0-9]+\.[0-9]+)",
        apt="libimage-exiftool-perl",
        fallback="метаданные будут удаляться средствами самих оптимизаторов",
    ),
    # Закрытые Windows-утилиты: только проверка наличия, без запуска.
    # Ограничения по платформе здесь нет намеренно: поиск на не-Windows стоит
    # один вызов which(), зато Windows-профиль можно проверять подделками на
    # любой машине — иначе он тестировался бы только на windows-раннере.
    "truepng": ToolSpec(
        name="truepng", aliases=("truepng",), never_execute=True,
        fallback="PNG будет оптимизирован через optipng",
    ),
    "deflopt": ToolSpec(
        name="deflopt", aliases=("deflopt",), never_execute=True,
        fallback="финальное уплотнение deflate будет пропущено",
    ),
}


@dataclass
class ResolvedTool:
    name: str
    path: Path
    version: str = ""
    caps: frozenset = frozenset()

    def has(self, cap: str) -> bool:
        return cap in self.caps


def platform_dir() -> str:
    """Имя каталога вида `linux-x86_64` для собранных бинарников."""
    system = {"nt": "win32"}.get(os.name, sys.platform)
    machine = platform.machine().lower() or "unknown"
    aliases = {"amd64": "x86_64", "x86-64": "x86_64", "arm64": "aarch64"}
    return "%s-%s" % (system, aliases.get(machine, machine))


def run(argv: Sequence[str], timeout: Optional[float] = None,
        capture: bool = False, cwd=None) -> subprocess.CompletedProcess:
    """Запустить внешний процесс, не показывая окон и не завязываясь на локаль."""
    env = dict(os.environ)
    env.update(_C_ENV)
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
        timeout=timeout,
        cwd=cwd,
        env=env,
        creationflags=_NO_WINDOW,
    )


class Aborted(Exception):
    """Прогон прерван пользователем; запускать новые процессы уже не нужно."""


class ProcessRegistry:
    """Учёт запущенных инструментов, чтобы их можно было прибить по Ctrl-C.

    Без этого прерывание пришлось бы ждать до конца текущего вызова, а
    `optipng -o7` на большом PNG считается минутами.
    """

    def __init__(self):
        self._live = set()
        self._lock = threading.Lock()
        self.aborted = False

    def run(self, argv: Sequence[str], timeout: Optional[float] = None,
            capture: bool = False) -> subprocess.CompletedProcess:
        if self.aborted:
            raise Aborted()
        env = dict(os.environ)
        env.update(_C_ENV)
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
            env=env,
            creationflags=_NO_WINDOW,
        )
        with self._lock:
            self._live.add(proc)
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            with self._lock:
                self._live.discard(proc)
        if self.aborted:
            raise Aborted()
        return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, None)

    def abort(self) -> None:
        self.aborted = True
        with self._lock:
            live = list(self._live)
        for proc in live:
            try:
                proc.terminate()
            except OSError:
                pass


class Toolbox:
    """Находит и опрашивает инструменты, кэшируя результат на весь прогон."""

    def __init__(self, config=None, app_dir: Optional[Path] = None):
        self.config = config
        self.app_dir = app_dir or app_directory()
        #: Искать только в ICATALYST_TOOLS_DIR, не заглядывая ни в PATH, ни в
        #: Tools/. Нужно тестам: иначе набор перестаёт быть герметичным и его
        #: результат зависит от того, что установлено на машине.
        self.hermetic = bool(os.environ.get("ICATALYST_TOOLS_ONLY"))
        self._cache: Dict[str, Optional[ResolvedTool]] = {}
        self._warned: Dict[str, str] = {}
        self._warn_lock = threading.Lock()
        self._search_dirs = self._build_search_dirs()

    # -- каталоги поиска ---------------------------------------------------

    def _build_search_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        # Тесты подсовывают поддельные инструменты именно здесь, и этот каталог
        # обязан побеждать всё, кроме явного пути к конкретному инструменту.
        injected = os.environ.get("ICATALYST_TOOLS_DIR")
        if injected:
            dirs.append(Path(injected))
        if self.hermetic:
            # Дальше не идём: тест, проверяющий поведение при отсутствии
            # инструмента, обязан получить это отсутствие независимо от того,
            # что установлено в системе.
            return dirs
        dirs.append(self.app_dir / "Tools" / "bin" / platform_dir())
        if os.name == "nt":
            dirs.append(self.app_dir / "Tools" / "apps")
            frozen = getattr(sys, "_MEIPASS", None)
            if frozen:
                dirs.append(Path(frozen) / "Tools" / "apps")
        return dirs

    # -- поиск -------------------------------------------------------------

    def _candidates(self, spec: ToolSpec) -> List[Path]:
        found: List[Path] = []
        env_key = "ICATALYST_%s" % spec.name.upper()
        explicit = os.environ.get(env_key)
        if explicit:
            found.append(Path(explicit))
        if self.config is not None:
            override = getattr(self.config, "tool_paths", {}).get(spec.name)
            if override:
                found.append(Path(override))
        exts = (".exe", ".bat", ".cmd", "") if os.name == "nt" else ("",)
        for directory in self._search_dirs:
            for alias in spec.aliases:
                for ext in exts:
                    found.append(directory / (alias + ext))
        if not self.hermetic:
            for alias in spec.aliases:
                which = shutil.which(alias)
                if which:
                    found.append(Path(which))
        return found

    def _probe(self, spec: ToolSpec, path: Path) -> Optional[ResolvedTool]:
        if not path.is_file():
            return None
        if spec.never_execute:
            return ResolvedTool(spec.name, path)
        if not os.access(str(path), os.X_OK) and os.name != "nt":
            return None
        try:
            proc = run([str(path), *spec.probe], timeout=10, capture=True)
        except (OSError, subprocess.SubprocessError):
            return None
        output = (proc.stdout or b"").decode("utf-8", "replace")
        if spec.accept and spec.accept not in output:
            return None
        caps_text = output
        if spec.caps_args:
            try:
                extra = run([str(path), *spec.caps_args], timeout=10, capture=True)
                caps_text += (extra.stdout or b"").decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                pass
        version = ""
        if spec.version_re:
            # Ищем версию в объединённом выводе: у jpegtran название и версия
            # печатаются по `-version`, а поддерживаемые ключи — по `-h`.
            match = re.search(spec.version_re, caps_text)
            if match:
                version = match.group(1)
        caps = frozenset(cap for cap, needle in spec.caps.items() if needle in caps_text)
        return ResolvedTool(spec.name, path, version, caps)

    def find(self, name: str) -> Optional[ResolvedTool]:
        """Найти инструмент или вернуть None. Результат кэшируется."""
        if name in self._cache:
            return self._cache[name]
        spec = TOOL_SPECS.get(name)
        if spec is None:
            raise KeyError("неизвестный инструмент: %s" % name)
        result = None
        if not spec.platforms or os.name in spec.platforms:
            for candidate in self._candidates(spec):
                # Кандидат может найтись, но не пройти зонд — тогда поиск
                # продолжается. Так отсеивается «нашёлся jpegtran, но не тот».
                result = self._probe(spec, candidate)
                if result is not None:
                    break
        self._cache[name] = result
        return result

    def require(self, name: str) -> ResolvedTool:
        tool = self.find(name)
        if tool is None:
            spec = TOOL_SPECS[name]
            searched = [str(p) for p in self._candidates(spec)]
            raise ToolMissing(name, searched, spec.apt, spec.build, spec.fallback)
        return tool

    def available(self, *names: str) -> bool:
        return all(self.find(name) is not None for name in names)

    # -- предупреждения ----------------------------------------------------

    def warn_once(self, key: str, message: str) -> None:
        """Запомнить предупреждение, показываемое ровно один раз за прогон.

        В 2.7 сообщение о пропущенном шаге печаталось бы на каждый файл и
        похоронило бы таблицу.
        """
        with self._warn_lock:
            self._warned.setdefault(key, message)

    def notes(self) -> List[str]:
        return list(self._warned.values())

    # -- отчёт для --doctor ------------------------------------------------

    def report(self) -> List[str]:
        lines = ["Инструменты (платформа: %s)" % platform_dir(), ""]
        width = max(len(n) for n in TOOL_SPECS)
        for name, spec in sorted(TOOL_SPECS.items()):
            if spec.platforms and os.name not in spec.platforms:
                continue
            tool = self.find(name)
            if tool is None:
                hint = spec.apt and ("apt: %s" % spec.apt) or spec.build and \
                    ("собрать: %s" % spec.build) or ""
                lines.append("  %-*s  не найден%s" % (width, name,
                                                      hint and "  (%s)" % hint))
                continue
            caps = ", ".join(sorted(tool.caps)) or "—"
            version = tool.version or "версия неизвестна"
            lines.append("  %-*s  %s  [%s]" % (width, name, version, caps))
            lines.append("  %-*s  %s" % (width, "", tool.path))
        return lines
