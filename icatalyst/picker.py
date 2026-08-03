"""Выбор каталога для результатов — замена `browsefolder.exe`.

Самое важное здесь — семантика отмены. В 2.7 `browsefolder.exe` при нажатии
Cancel не печатал ничего, `%outdir%` оставался пустым, и это означало
**перезаписать оригиналы** (`iCatalyst.bat:163`). Между пользователем и заменой
его файлов стояла ровно одна строка текста в диалоге, поэтому она сохранена
дословно.

Отсюда же трёхзначный результат вместо `str | None`: именно неразличение
«отмены» и «ошибки диалога» — тот способ, которым «Cancel = перезаписать»
ломается случайно.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

#: Заголовок и пояснение сохранены из `iCatalyst.bat:163` дословно.
DIALOG_TITLE = "Image Catalyst"
DIALOG_TEXT = ("Choose directory to save images to. "
               "Click 'Cancel' to replace original images with optimized versions.")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class Choice(Enum):
    #: Пользователь выбрал каталог.
    DIR = "dir"
    #: Пользователь отказался — перезаписываем оригиналы.
    IN_PLACE = "in-place"
    #: Пользователь прервал работу (Ctrl-C, закрытие).
    ABORT = "abort"


@dataclass(frozen=True)
class PickResult:
    choice: Choice
    path: Optional[Path] = None
    #: Сообщение для блока предупреждений (например, диалог не запустился).
    note: str = ""

    @property
    def in_place(self) -> bool:
        return self.choice is Choice.IN_PLACE


IN_PLACE = PickResult(Choice.IN_PLACE)
ABORT = PickResult(Choice.ABORT)


# ---------------------------------------------------------------------------
# Отдельные механизмы
# ---------------------------------------------------------------------------

def _run(argv: List[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # GTK и Qt любят сорить в stderr
        timeout=timeout, creationflags=_NO_WINDOW,
    )


def _pick_zenity(initial: Optional[Path]) -> PickResult:
    argv = ["zenity", "--file-selection", "--directory",
            "--title=%s" % DIALOG_TITLE, "--text=%s" % DIALOG_TEXT]
    if initial:
        argv.append("--filename=%s%s" % (initial, os.sep))
    try:
        proc = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return PickResult(Choice.IN_PLACE, note="zenity не запустился (%s)" % exc)
    if proc.returncode == 0:
        path = proc.stdout.decode("utf-8", "surrogateescape").strip()
        if path:
            return PickResult(Choice.DIR, Path(path))
        return IN_PLACE
    if proc.returncode == 1:
        # Штатная отмена.
        return IN_PLACE
    return PickResult(Choice.IN_PLACE,
                      note="zenity завершился с кодом %d" % proc.returncode)


def _pick_kdialog(initial: Optional[Path]) -> PickResult:
    argv = ["kdialog", "--title", DIALOG_TITLE,
            "--getexistingdirectory", str(initial or Path.home())]
    try:
        proc = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return PickResult(Choice.IN_PLACE, note="kdialog не запустился (%s)" % exc)
    if proc.returncode == 0:
        path = proc.stdout.decode("utf-8", "surrogateescape").strip()
        if path:
            return PickResult(Choice.DIR, Path(path))
    return IN_PLACE


def _pick_osascript(initial: Optional[Path]) -> PickResult:
    script = 'POSIX path of (choose folder with prompt "%s")' % DIALOG_TEXT
    try:
        proc = _run(["osascript", "-e", script])
    except (OSError, subprocess.SubprocessError) as exc:
        return PickResult(Choice.IN_PLACE, note="osascript не запустился (%s)" % exc)
    if proc.returncode == 0:
        path = proc.stdout.decode("utf-8", "surrogateescape").strip()
        if path:
            return PickResult(Choice.DIR, Path(path))
    return IN_PLACE


def _tk_importable() -> bool:
    """Проверить tkinter в отдельном процессе.

    В процессе делать это нельзя: сломанный Tk или недоступный дисплей способны
    оборвать интерпретатор целиком, а не бросить исключение. К тому же на стоковых
    Ubuntu и Mint пакет `python3-tk` не установлен, и импорт просто падает.
    """
    if getattr(sys, "frozen", False):
        # В собранном PyInstaller виде Tk лежит внутри, а `-c` работать не будет.
        return True
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _pick_tk(initial: Optional[Path]) -> PickResult:
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:  # ImportError и всё, что Tk придумает
        return PickResult(Choice.IN_PLACE, note="tkinter недоступен (%s)" % exc)
    try:
        root = tkinter.Tk()
        root.withdraw()
        try:
            # На Windows это нативный диалог оболочки, то есть прямая замена
            # browsefolder.exe без единого дополнительного бинарника.
            path = filedialog.askdirectory(
                title=DIALOG_TITLE, mustexist=False,
                initialdir=str(initial) if initial else None)
        finally:
            root.destroy()
    except Exception as exc:
        return PickResult(Choice.IN_PLACE, note="диалог tkinter не открылся (%s)" % exc)
    if path:
        return PickResult(Choice.DIR, Path(path))
    return IN_PLACE


def _pick_terminal(initial: Optional[Path]) -> PickResult:
    if not _stdin_is_tty():
        # Спросить некого. Молча перезаписать оригиналы — недопустимо: это
        # необратимо и пользователь об этом не просил. В 2.7 пустой `%outdir%`
        # означал именно перезапись, но там до этого места доходил живой
        # диалог, а не пакетный запуск.
        return PickResult(
            Choice.ABORT,
            note="каталог для результатов не задан, а спросить негде: "
                 "укажите \"/outdir:путь\" или /outdir:false")
    rule = "-" * 79
    print(rule)
    print(" Choose directory to save images to.")
    print(" Leave empty to replace original images with optimized versions.")
    print(rule)
    try:
        raw = input(" Output directory: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ABORT
    print()
    if not raw:
        # В терминале «просто Enter» слишком легко нажать вслепую, поэтому
        # здесь — и только здесь — спрашиваем подтверждение. В графическом
        # диалоге такого вопроса быть не должно: он сломал бы привычный поток.
        try:
            answer = input(" Replace original images with optimized? [y/N]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ABORT
        print()
        return IN_PLACE if answer[:1].lower() in ("y", "д") else ABORT
    return PickResult(Choice.DIR, Path(os.path.expanduser(raw)))


# ---------------------------------------------------------------------------
# Выбор механизма
# ---------------------------------------------------------------------------

_BACKENDS = {
    "zenity": _pick_zenity,
    "kdialog": _pick_kdialog,
    "osascript": _pick_osascript,
    "tk": _pick_tk,
    "terminal": _pick_terminal,
}


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def backend_chain() -> List[str]:
    """Порядок опроса механизмов для текущей системы."""
    if os.name == "nt":
        # Tk на Windows надёжен и даёт нативный диалог оболочки.
        return ["tk", "terminal"]
    if sys.platform == "darwin":
        return ["osascript", "terminal"]
    if not has_display():
        # Без дисплея графический диалог не пробуем вообще: zenity в такой
        # ситуации несколько секунд думает и всё равно падает.
        return ["terminal"]
    chain = []
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if "KDE" in desktop and shutil.which("kdialog"):
        chain.append("kdialog")
    if shutil.which("zenity"):
        chain.append("zenity")
    if shutil.which("kdialog") and "kdialog" not in chain:
        chain.append("kdialog")
    # tkinter на Linux — последний кандидат, а не первый: python3-tk на
    # стоковых Ubuntu и Mint не установлен.
    if _tk_importable():
        chain.append("tk")
    chain.append("terminal")
    return chain


def pick_directory(preferred: str = "auto",
                   initial: Optional[Path] = None) -> PickResult:
    """Спросить каталог. Отмена означает «перезаписать оригиналы»."""
    forced = (os.environ.get("ICATALYST_PICKER") or preferred or "auto").lower()
    if forced == "none":
        return PickResult(Choice.IN_PLACE,
                          note="диалог выбора каталога отключён настройкой picker=none")
    if forced != "auto":
        backend = _BACKENDS.get(forced)
        if backend is None:
            return PickResult(Choice.IN_PLACE,
                              note="неизвестный механизм выбора каталога: %s" % forced)
        return backend(initial)

    notes = []
    for name in backend_chain():
        result = _BACKENDS[name](initial)
        if result.note and result.choice is not Choice.ABORT:
            # Механизм сломался — пробуем следующий, а не решаем за пользователя.
            notes.append(result.note)
            continue
        # Всё остальное — это ответ: выбранный каталог, штатная отмена
        # (перезаписать оригиналы) или прерывание.
        if notes:
            result = PickResult(result.choice, result.path,
                                "; ".join(notes + ([result.note] if result.note else [])))
        return result
    return PickResult(Choice.ABORT, note="; ".join(notes)
                      or "не удалось спросить каталог для результатов")
