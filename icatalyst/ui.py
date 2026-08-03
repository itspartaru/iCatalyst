"""Интерактивная часть: меню режимов, запрос каталога, пауза на выходе.

Формулировки, нумерация и рамки сохранены дословно из `:png`, `:jpeg` и `:gif`
(`iCatalyst.bat:592-653`) — это тот же продукт, и мышечная память
пользователей должна работать. Модуль отделён от `report.py` именно потому, что
всё здесь принципиально не проверяется тестами, а таблица — проверяется.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence

from . import APP_NAME

#: Меню по формату: заголовок и пункты. Порядок и номера — как в 2.7.
MENUS = {
    "png": ("PNG optimization mode:", ((1, "Advanced"), (2, "Xtreme"), (0, "Skip"))),
    "jpg": ("JPEG otimization mode:",  # опечатка сохранена: она в 2.7 с 2010 года
            ((1, "Baseline"), (2, "Progressive"), (3, "Default"), (0, "Skip"))),
    "gif": ("GIF optimization mode:", ((1, "Default"), (0, "Skip"))),
}


def is_interactive() -> bool:
    return _isatty(sys.stdin) and _isatty(sys.stdout)


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def clear_screen() -> None:
    """Очистить экран, но только когда мы действительно владеем консолью.

    В 2.7 то же условие проверялось через `%CMDCMDLINE%` (`:clearscreen`):
    очищать чужой терминал, в который просто перенаправили вывод, нельзя.
    """
    if not is_interactive():
        return
    sys.stdout.write("\x1b[2J\x1b[H" if os.name != "nt" else "\n" * 3)
    sys.stdout.flush()


def ask_mode(fmt: str, notes: Optional[str] = None) -> int:
    """Показать меню и вернуть выбранный режим.

    Меню вызывается только для форматов, реально присутствующих во входных
    данных, — это поведение 2.7, и его надо сохранить.
    """
    title, items = MENUS[fmt]
    codes = [code for code, _ in items]
    low, high = min(codes), max(codes)
    while True:
        print(" " + "-" * len(title))
        print(" " + title)
        print(" " + "-" * len(title))
        print()
        for code, label in items:
            suffix = ""
            if notes and code == 2 and fmt == "png":
                suffix = "  (%s)" % notes
            print(" [%d] %s%s" % (code, label, suffix))
            print()
        print(" " + "-" * 36)
        try:
            raw = input("#Select mode and press Enter [%d-%d]: " % (low, high))
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(1)
        print(" " + "-" * 36)
        print()
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if value in codes:
            return value


def pause(enabled: bool) -> None:
    """Задержать окно перед закрытием.

    В 2.7 `:dopause` смотрел на `%CMDCMDLINE%`, чтобы паузу видел тот, кто
    запустил двойным щелчком, и не видел тот, кто вызвал из скрипта.
    """
    if not enabled or not is_interactive():
        return
    try:
        input("Press Enter to continue . . . ")
    except (EOFError, KeyboardInterrupt):
        pass


def should_pause() -> bool:
    """Определить, нужна ли пауза, не угадывая.

    На Windows — по числу процессов, подключённых к консоли: если мы один, то
    консоль наша, и закрыв её, пользователь не увидит отчёт. На Linux — по
    переменной, которую выставляет `.desktop`-файл.
    """
    if os.environ.get("ICATALYST_LAUNCHED_FROM_DESKTOP"):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        buffer = (ctypes.c_ulong * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buffer, 8)
        return count == 1
    except Exception:
        return False


def banner(lines: Sequence[str]) -> None:
    for line in lines:
        print(line)


def help_text(version: str) -> List[str]:
    """Справка. Текст сохранён из `:helpmsg` с поправками на новые ключи."""
    rule = "-" * 79
    return [
        rule,
        " %s - lossless PNG, JPEG and GIF image optimization / compression" % APP_NAME,
        "",
        " %s version %s by Lorents & Res2001 (2010-2026)" % (APP_NAME, version),
        " https://github.com/lorents17/iCatalyst",
        rule,
        "",
        " Usage: icatalyst [options] [add directories \\ add files]",
        "",
        " Options:",
        "",
        " /png:#\tPNG optimization mode (Non-Interlaced):",
        "\t1 - Compression level - Advanced",
        "\t2 - Compression level - Xtreme",
        "\t0 - Skip",
        "",
        " /jpg:#\tJPEG optimization mode:",
        "\t1 - Encoding Process - Baseline",
        "\t2 - Encoding Process - Progressive",
        "\t3 - use settings of original image",
        "\t0 - Skip",
        "",
        " /gif:#\tGIF optimization mode:",
        "\t1 - use settings of original image",
        "\t0 - Skip",
        "",
        ' "/outdir:#" image saving options:',
        "\ttrue  - ask where to save images (default)",
        "\tfalse - replace original image with optimized",
        '\t"full path to directory" - specify directory to save images to.',
        '\tfor example: "/outdir:C:\\temp". If the destination directory',
        "\tdoes not exist, it will be created automatically.",
        "",
        " Additional options:",
        "",
        " --config FILE\t\tuse this config.ini instead of Tools/config.ini",
        " --verify\t\tcompare PNG pixel data before and after (slow, thorough)",
        " --strict-lossless\tforbid changing RGB under fully transparent pixels",
        " --tsv\t\t\tmachine-readable output instead of the table",
        " --doctor\t\tshow which tools were found and what will be run",
        " --width N\t\tforce table width",
        " --picker NAME\t\tauto, tk, zenity, kdialog, osascript, terminal or none",
        " --threads N\t\tnumber of parallel jobs (0 or absent = CPU count)",
        " --stream\t\tprint rows in completion order instead of input order",
        " --no-pause\t\tdo not wait for a key press at the end",
        " --version\t\tprint version and exit",
        "",
        " Add directories \\ Add files:",
        " - Specify full image paths and / or paths to directories containing images.",
        '   For example: "C:\\Images" "C:\\logo.png"',
        " - Any characters are allowed in paths, including national alphabets,",
        "   spaces and punctuation. This is a change from version 2.7.",
        " - Images in sub-directories are optimized recursively.",
        "",
        " Examples:",
        ' icatalyst /gif:1 "/outdir:C:\\photos" "C:\\images"',
        ' icatalyst /png:2 /jpg:2 "/outdir:true" "C:\\images"',
        rule,
    ]
