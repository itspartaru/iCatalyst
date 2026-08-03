#!/usr/bin/env python3
"""Получение внешних инструментов: скачать готовое или собрать из исходников.

Реализация на Python, а не на shell, по трём причинам. Она использует тот же
`icatalyst.toolbox`, что и приложение, поэтому «скрипт говорит, что собралось» и
«программа говорит, что работает» не могут разъехаться. Она не спотыкается о
`.gitattributes`: `.sh`, извлечённый с CRLF, умирает с `bad interpreter:
/bin/bash^M`. И проверка sha256, распаковка и штампы в ней куда надёжнее, чем в
shell.

По умолчанию — `--download`: пользователь не обязан ничего компилировать.
Сборка из исходников остаётся первоклассным путём и включается явно.

Скрипт никогда не вызывает sudo и никогда не пишет вне `Tools/build` и
`Tools/bin` — отсутствующие системные пакеты он только называет.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from icatalyst.toolbox import TOOL_SPECS, Toolbox, platform_dir  # noqa: E402

TOOLS_DIR = REPO_ROOT / "Tools"
LOCK_FILE = TOOLS_DIR / "tools.lock.json"
BUILD_DIR = TOOLS_DIR / "build"
BIN_DIR = TOOLS_DIR / "bin" / platform_dir()
STAMP_DIR = BUILD_DIR / "stamp"
DL_DIR = BUILD_DIR / "dl"


class BuildError(Exception):
    pass


def _configure_stdout() -> None:
    """Перевести вывод в UTF-8.

    Скрипт печатает по-русски, а на windows-раннере stdout — cp1252, и первое же
    сообщение падало с UnicodeEncodeError. Приложение делает то же в
    `report.configure_streams`.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


_configure_stdout()


def log(message: str) -> None:
    sys.stdout.write("%s\n" % message)
    sys.stdout.flush()


def load_lock() -> Dict:
    if not LOCK_FILE.is_file():
        raise BuildError("не найден файл пинов: %s" % LOCK_FILE)
    with open(LOCK_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Штампы: идемпотентность
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp_path(name: str, pin: str) -> Path:
    tag = hashlib.sha256(("%s|%s|%s" % (name, pin, platform_dir())).encode()).hexdigest()[:16]
    return STAMP_DIR / ("%s-%s.json" % (name, tag))


def _is_current(name: str, pin: str, target: Path) -> bool:
    """Шаг пропускается, только если совпало всё: штамп, файл и его хеш."""
    stamp = _stamp_path(name, pin)
    if not stamp.is_file() or not target.is_file():
        return False
    try:
        with open(stamp, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return False
    return saved.get("sha256") == _sha256_file(target)


def _write_stamp(name: str, pin: str, target: Path, extra: Optional[Dict] = None) -> None:
    STAMP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool": name,
        "pin": pin,
        "platform": platform_dir(),
        "path": str(target),
        "sha256": _sha256_file(target),
    }
    payload.update(extra or {})
    with open(_stamp_path(name, pin), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _install(source: Path, name: str) -> Path:
    """Положить готовый бинарник в Tools/bin через временный файл."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / name
    staging = BIN_DIR / (".%s.partial" % name)
    shutil.copyfile(source, staging)
    staging.chmod(0o755)
    os.replace(staging, target)
    return target


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------

def _fetch(url: str, expected_sha256: str) -> Path:
    DL_DIR.mkdir(parents=True, exist_ok=True)
    cached = DL_DIR / url.rsplit("/", 1)[-1]
    if cached.is_file() and _sha256_file(cached) == expected_sha256:
        log("    из кэша: %s" % cached.name)
        return cached
    log("    качаю %s" % url)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            blob = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise BuildError("не удалось скачать %s: %s" % (url, exc))
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected_sha256:
        # Не сохраняем и не распаковываем: это исполняемый код из сети.
        raise BuildError(
            "хеш не совпал для %s\n  ожидался %s\n  получен  %s"
            % (url, expected_sha256, actual))
    with open(cached, "wb") as fh:
        fh.write(blob)
    return cached


def _extract_member(archive: Path, member: str, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    destination = into / Path(member).name
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            picked = member if member in names else _match_tail(names, member)
            with zf.open(picked) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return destination
    with tarfile.open(archive) as tf:
        names = tf.getnames()
        picked = member if member in names else _match_tail(names, member)
        extracted = tf.extractfile(picked)
        if extracted is None:
            raise BuildError("в архиве %s нет файла %s" % (archive.name, member))
        with extracted as src, open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return destination


def _match_tail(names: Sequence[str], member: str) -> str:
    """Найти файл по имени, если каталог в архиве назван иначе, чем в пине."""
    tail = Path(member).name
    for name in names:
        if Path(name).name == tail:
            return name
    raise BuildError("в архиве нет файла с именем %s" % tail)


def download(lock: Dict, only: Optional[List[str]]) -> List[str]:
    problems = []
    for name, entry in sorted(lock.get("downloads", {}).items()):
        if only and name not in only:
            continue
        artifact = entry["artifacts"].get(platform_dir())
        if artifact is None:
            log("  %s: для платформы %s готовой сборки нет" % (name, platform_dir()))
            continue
        pin = "%s@%s" % (entry["version"], artifact["sha256"])
        target = BIN_DIR / artifact["install_as"]
        if _is_current(name, pin, target):
            log("  %s %s: уже на месте" % (name, entry["version"]))
            continue
        log("  %s %s:" % (name, entry["version"]))
        try:
            archive = _fetch(artifact["url"], artifact["sha256"])
            with tempfile.TemporaryDirectory() as tmp:
                extracted = _extract_member(archive, artifact["member"], Path(tmp))
                installed = _install(extracted, artifact["install_as"])
            _write_stamp(name, pin, installed,
                         {"version": entry["version"], "source": artifact["url"]})
            log("    установлен: %s" % installed)
        except BuildError as exc:
            problems.append("%s: %s" % (name, exc))
            log("    ОШИБКА: %s" % exc)
    return problems


# ---------------------------------------------------------------------------
# Сборка из исходников
# ---------------------------------------------------------------------------

def _which_all(names: Sequence[str]) -> List[str]:
    return [name for name in names if shutil.which(name) is None]


def _run(argv: Sequence[str], cwd: Optional[Path] = None) -> None:
    log("    $ %s" % " ".join(str(a) for a in argv))
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise BuildError("команда завершилась с кодом %d: %s"
                         % (proc.returncode, " ".join(str(a) for a in argv)))


def _apply_patches(entry: Dict, source: Path) -> None:
    directory = entry.get("patches")
    if not directory:
        return
    patch_dir = REPO_ROOT / directory
    if not patch_dir.is_dir():
        return
    for patch in sorted(patch_dir.glob("*.patch")):
        check = subprocess.run(["git", "apply", "--check", str(patch)],
                               cwd=str(source),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode != 0:
            log("    патч уже применён или неприменим, пропускаю: %s" % patch.name)
            continue
        _run(["git", "apply", str(patch)], cwd=source)


def build(lock: Dict, only: Optional[List[str]]) -> List[str]:
    problems = []
    for name, entry in sorted(lock.get("builds", {}).items()):
        if only and name not in only:
            continue
        pin = "%s@%s" % (entry["repo"], entry["tag"])
        target = BIN_DIR / entry["install_as"]
        if _is_current(name, pin, target):
            log("  %s %s: уже собран" % (name, entry["tag"]))
            continue

        # Все недостающие требования сообщаются сразу: узнать об отсутствии
        # nasm через сорок секунд компиляции — худший из возможных вариантов.
        missing = _which_all(["git", "cmake"])
        if missing:
            problems.append(
                "%s: не хватает %s (установить: sudo apt install %s)"
                % (name, ", ".join(missing), " ".join(entry.get("apt", missing))))
            log("  %s: пропущен, нет %s" % (name, ", ".join(missing)))
            continue

        log("  %s %s:" % (name, entry["tag"]))
        source = BUILD_DIR / "src" / name
        obj = BUILD_DIR / "obj" / name
        try:
            if not (source / ".git").is_dir():
                shutil.rmtree(source, ignore_errors=True)
                source.parent.mkdir(parents=True, exist_ok=True)
                clone = ["git", "clone", "--depth", "1", "--branch", entry["tag"]]
                if entry.get("submodules"):
                    clone += ["--recurse-submodules", "--shallow-submodules"]
                clone += [entry["repo"], str(source)]
                _run(clone)
            _apply_patches(entry, source)

            args = list(entry.get("cmake_args", []))
            if name == "mozjpeg":
                # SIMD требует nasm. Для jpegtran потеря незначима: он работает
                # в энтропийной области и DCT не касается.
                args.append("-DWITH_SIMD=%d"
                            % (0 if shutil.which("nasm") is None else 1))
                if shutil.which("nasm") is None:
                    log("    nasm не найден, собираю без SIMD "
                        "(для jpegtran это несущественно)")
            _run(["cmake", "-S", str(source), "-B", str(obj),
                  "-G", "Unix Makefiles", *args])
            jobs = str(os.cpu_count() or 1)
            build_cmd = ["cmake", "--build", str(obj), "-j", jobs]
            if entry.get("target"):
                build_cmd += ["--target", entry["target"]]
            _run(build_cmd)

            produced = _find_built(obj, entry["target"], entry["install_as"])
            installed = _install(produced, entry["install_as"])
            _write_stamp(name, pin, installed, {
                "tag": entry["tag"],
                "repo": entry["repo"],
                "submodules": _submodule_state(source) if entry.get("submodules") else {},
            })
            log("    установлен: %s" % installed)
        except BuildError as exc:
            problems.append("%s: %s" % (name, exc))
            log("    ОШИБКА: %s" % exc)
    return problems


def _find_built(obj: Path, target: str, install_as: str = "") -> Path:
    """Найти собранный файл по имени цели или по имени установки.

    Имена расходятся: у mozjpeg цель называется `jpegtran-static`, а upstream
    ставит её переименованной в `jpegtran`. Ищем по обоим, чтобы переименование
    вверху по течению не ломало сборку молча.
    """
    names = [n for n in (target, install_as) if n]
    for name in names:
        for candidate in (obj / name, obj / (name + ".exe")):
            if candidate.is_file():
                return candidate
    for name in names:
        matches = [p for p in obj.rglob(name) if p.is_file()] + \
                  [p for p in obj.rglob(name + ".exe") if p.is_file()]
        if matches:
            return matches[0]
    raise BuildError("сборка прошла, но исполняемый файл (%s) не найден в %s"
                     % (" или ".join(names), obj))


def _submodule_state(source: Path) -> Dict[str, str]:
    proc = subprocess.run(["git", "submodule", "status"], cwd=str(source),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    state = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            state[parts[1]] = parts[0].lstrip("+-U")
    return state


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

#: Максимальный номер режима по формату.
_MODES = {"png": 2, "jpg": 3, "gif": 1}


def check() -> int:
    """Напечатать матрицу возможностей. Это и есть результат работы скрипта.

    Проверяется доступность **режимов**, а не наличие конкретных утилит. Раньше
    список обязательных был зашит как optipng/gifsicle/jpegtran, и на Windows
    скрипт падал из-за отсутствия optipng — при том что там его роль играет
    TruePNG, вложенный в репозиторий, и все режимы прекрасно работали.
    """
    from icatalyst import config as cfgmod
    from icatalyst import recipes

    # Читаем настоящую конфигурацию, а не значения по умолчанию: ключ `profile`
    # меняет набор инструментов, и отчёт обязан показывать то, что программа
    # действительно выполнит.
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError:
        cfg = cfgmod.Config()
    toolbox = Toolbox(cfg)
    log("Image Catalyst — доступные инструменты      платформа: %s" % platform_dir())
    log("")
    for line in toolbox.report():
        log(line)
    log("")

    unavailable = []
    for fmt, top in sorted(_MODES.items()):
        modes = []
        for mode in range(1, top + 1):
            recipe = recipes.build(fmt, mode, cfg)
            if recipe is None:
                continue
            chains = recipes.runnable_chains(recipe, toolbox)
            if not chains:
                spare = recipes.fallback_recipe(fmt, mode, cfg)
                if spare is not None:
                    chains = recipes.runnable_chains(spare, toolbox)
            modes.append((recipe.label, bool(chains)))
        ready = [label for label, ok in modes if ok]
        missing = [label for label, ok in modes if not ok]
        if ready:
            log("  %-4s доступно: %s%s"
                % (fmt.upper(), ", ".join(ready),
                   ("; недоступно: " + ", ".join(missing)) if missing else ""))
        else:
            unavailable.append(fmt.upper())
            log("  %-4s НЕДОСТУПЕН" % fmt.upper())

    log("")
    if unavailable:
        log("Форматы без единого рабочего режима: %s" % ", ".join(unavailable))
        needed = sorted({spec.apt for spec in TOOL_SPECS.values()
                         if spec.apt and toolbox.find(spec.name) is None})
        if needed and os.name != "nt":
            log("  sudo apt install %s" % " ".join(needed))
        elif os.name == "nt":
            log("  python Tools/build_tools.py --download")
        return 1
    log("Все форматы работают.")
    optional = [name for name in ("oxipng", "zopflipng", "pngwolf", "advdef")
                if toolbox.find(name) is None]
    if optional:
        log("Необязательные, улучшают сжатие: %s" % ", ".join(optional))
    return 0


def report_built(lock: Dict) -> int:
    """Перечислить, что реально лежит в Tools/bin, и чего не хватает.

    Отделено от `--check` намеренно. `--check` отвечает на вопрос «можно ли этим
    пользоваться» и потому падает, если формат недоступен — это правильно для
    пользователя. А работа CI, собирающая артефакт с инструментами, apt-пакеты
    не устанавливает и падать из-за их отсутствия не должна: её задача — честно
    сказать, что попало в архив.
    """
    expected = sorted(set(lock.get("downloads", {})) | set(lock.get("builds", {})))
    present, missing = [], []
    for name in expected:
        entry = (lock.get("downloads", {}).get(name)
                 or lock.get("builds", {}).get(name))
        install_as = entry.get("install_as")
        if install_as is None:
            artifact = entry.get("artifacts", {}).get(platform_dir())
            install_as = artifact["install_as"] if artifact else name
        path = BIN_DIR / install_as
        if path.is_file():
            present.append((name, path, _sha256_file(path)))
        else:
            missing.append(name)

    log("Артефакты в %s" % BIN_DIR)
    for name, path, digest in present:
        log("  %-10s %10d Б  sha256:%s" % (name, path.stat().st_size, digest))
    if missing:
        # Ни при каких условиях не молча: отсутствующий инструмент означает, что
        # соответствующий режим у пользователя деградирует.
        log("")
        log("НЕ СОБРАНО: %s" % ", ".join(missing))
        for name in missing:
            entry = lock.get("builds", {}).get(name)
            if entry:
                log("  %s: сборка из %s@%s не удалась — смотрите лог шага сборки"
                    % (name, entry["repo"], entry["tag"]))
    if not present:
        log("")
        log("В артефакт не попало ни одного инструмента.")
        return 1
    return 0


def print_apt(lock: Dict) -> int:
    packages = set()
    for name, spec in TOOL_SPECS.items():
        if spec.apt:
            packages.add(spec.apt)
    log("# оптимизаторы из пакетов")
    log("sudo apt install %s" % " ".join(sorted(packages)))
    build_packages = set()
    for entry in lock.get("builds", {}).values():
        build_packages.update(entry.get("apt", []))
    log("")
    log("# только для сборки из исходников (--build)")
    log("sudo apt install %s" % " ".join(sorted(build_packages)))
    return 0


def clean() -> int:
    """Удалить только промежуточные результаты: bin и apps не трогаются."""
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
        log("удалено: %s" % BUILD_DIR)
    else:
        log("нечего удалять")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Получение внешних инструментов Image Catalyst")
    parser.add_argument("--download", action="store_true",
                        help="скачать готовые сборки (по умолчанию)")
    parser.add_argument("--build", action="store_true",
                        help="собрать из исходников то, чего нет в пакетах")
    parser.add_argument("--check", action="store_true",
                        help="показать, что найдено; код 1, если формат недоступен")
    parser.add_argument("--report-built", action="store_true",
                        help="перечислить собранные артефакты; не требует apt-пакетов")
    parser.add_argument("--print-apt", action="store_true",
                        help="напечатать строку установки пакетов и выйти")
    parser.add_argument("--clean", action="store_true",
                        help="удалить Tools/build")
    parser.add_argument("--only", action="append", metavar="ИНСТРУМЕНТ",
                        help="ограничиться этим инструментом (можно повторять)")
    args = parser.parse_args(argv)

    try:
        lock = load_lock()
    except BuildError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    if args.print_apt:
        return print_apt(lock)
    if args.clean:
        return clean()
    if args.report_built:
        return report_built(lock)
    if args.check:
        return check()

    problems: List[str] = []
    do_download = args.download or not args.build
    if do_download:
        log("Скачиваю готовые сборки:")
        problems += download(lock, args.only)
    if args.build:
        log("Собираю из исходников:")
        problems += build(lock, args.only)

    log("")
    status = check()
    if problems:
        log("")
        log("Проблемы:")
        for problem in problems:
            log("  - %s" % problem)
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
