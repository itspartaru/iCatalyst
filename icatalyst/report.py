"""Отчёт: таблица, итоги, группы ошибок, заголовок окна.

Функции здесь чистые — байты на входе, строки на выходе, — чтобы таблицу можно
было проверять эталонными файлами без терминала вообще. Всё интерактивное
живёт в `ui.py`.

Геометрия взята из `iCatalyst.bat` дословно: разделитель 79 символов
(строка 15), строка данных 78 (строки 221-222, 410). Любое расхождение сдвигает
всю таблицу, поэтому ширины вынесены в константы и покрыты тестом.

Вся арифметика с размерами тут в обычных целых Python. В 2.7 на её месте было
около 150 строк (`:stepcalc`, `:fincalc`, `:division2`, `:finprepsize`),
боровшихся с переполнением 32-битного `set /a`: накопители байтов
масштабировались на ходу при превышении 100 МБ. Ничего этого больше не нужно, и
итоги свыше 2 ГБ становятся корректными сами собой.
"""

from __future__ import annotations

import os
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, TextIO, Tuple

from . import APP_NAME, RULE_WIDTH

NAME_W = 31
ORIG_W = 10
OPT_W = 11
SAVE_W = 11
PCT_W = 10

#: Строки ошибок в 2.7 использовали другую ширину имени — 30 (`:printfileerr`).
ERR_NAME_W = 30
ERR_MSG_W = 45

RULE = "-" * RULE_WIDTH

KB = 1024
MB = KB * KB
GB = MB * KB
TB = GB * KB

_UNITS = (("TB", TB), ("GB", GB), ("MB", MB), ("KB", KB), ("B", 1))

#: Порядок форматов в отчёте и в заголовке окна — как в 2.7.
FORMAT_ORDER = ("PNG", "JPG", "GIF")


# ---------------------------------------------------------------------------
# Ширина в терминальных колонках
# ---------------------------------------------------------------------------

def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Ширина строки в колонках терминала.

    Legacy считал символы, поэтому имена с иероглифами или эмодзи разъезжали
    таблицу. Тестовый корпус содержит и то и другое.
    """
    return sum(char_width(ch) for ch in text)


def crop_display(text: str, limit: int) -> str:
    if display_width(text) <= limit:
        return text
    out = []
    used = 0
    for ch in text:
        width = char_width(ch)
        if used + width > limit:
            break
        out.append(ch)
        used += width
    return "".join(out)


def pad_display(text: str, width: int) -> str:
    text = crop_display(text, width)
    return text + " " * (width - display_width(text))


def rjust_display(text: str, width: int) -> str:
    text = crop_display(text, width)
    return " " * (width - display_width(text)) + text


# ---------------------------------------------------------------------------
# Размеры и проценты
# ---------------------------------------------------------------------------

def pick_unit(value: int) -> Tuple[str, int]:
    magnitude = abs(value)
    for name, divisor in _UNITS:
        if magnitude >= divisor:
            return name, divisor
    return "B", 1


def format_size(value: int, unit: Optional[Tuple[str, int]] = None) -> str:
    """Отформатировать размер так же, как `:prepsize`/`:prepsize2`.

    Байты печатаются целым числом, всё остальное — с двумя знаками после
    запятой и **усечением**, а не округлением (в batch было целочисленное
    деление).
    """
    if unit is None:
        unit = pick_unit(value)
    name, divisor = unit
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if divisor == 1:
        return "%s%d B" % (sign, magnitude)
    whole, remainder = divmod(magnitude, divisor)
    return "%s%d.%02d %s" % (sign, whole, remainder * 100 // divisor, name)


def format_percent(delta: int, original: int) -> str:
    """Процент изменения с двумя знаками, усечением и сохранением знака."""
    if original <= 0:
        return "0.00%"
    sign = "-" if delta < 0 else ""
    hundredths = abs(delta) * 10000 // original
    return "%s%d.%02d%%" % (sign, hundredths // 100, hundredths % 100)


def crop_filename(name: str, limit: int = NAME_W) -> str:
    """Укоротить имя, сохранив расширение: `длинное_имя..png`.

    Повторяет `:cropfilename` (`iCatalyst.bat:482`), но считает колонки, а не
    символы.
    """
    if display_width(name) <= limit:
        return name
    ext = os.path.splitext(name)[1]
    keep = limit - display_width(ext) - 2
    if keep <= 0:
        return crop_display(name, limit)
    return crop_display(name, keep) + ".." + ext


# ---------------------------------------------------------------------------
# Агрегаты по форматам
# ---------------------------------------------------------------------------

@dataclass
class FormatTotals:
    label: str = ""
    total_files: int = 0
    done_files: int = 0
    errors: int = 0
    original: int = 0
    optimized: int = 0

    @property
    def delta(self) -> int:
        return self.optimized - self.original


class Progress:
    """Счётчики выполненного по форматам для заголовка окна.

    Заменяет файлы `count<FMT>.<N>` из 2.7, куда рабочие процессы дописывали по
    строке `1\\r\\n`, а главный получал количество, **деля размер файла на 3**
    (`iCatalyst.bat:660-662`).

    Инкремент делает рабочий поток сразу по завершении файла, а не главный при
    печати строки: иначе процент стоял бы на месте, пока таблица ждёт своей
    очереди на медленном файле.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items: Dict[str, List] = {}

    def add(self, key: str, label: str, total: int) -> None:
        with self._lock:
            self._items[key.upper()] = [label, 0, total]

    def bump(self, key: str) -> None:
        with self._lock:
            item = self._items.get(key.upper())
            if item is not None:
                item[1] += 1

    def snapshot(self) -> Dict[str, Tuple[str, int, int]]:
        with self._lock:
            return {key: (item[0], item[1], item[2])
                    for key, item in self._items.items()}


@dataclass
class Summary:
    formats: Dict[str, FormatTotals] = field(default_factory=dict)
    #: Списки имён по корзинам ошибок, как в 2.7.
    unsupported: List[str] = field(default_factory=list)
    notfound: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)

    def bucket(self, fmt: str) -> FormatTotals:
        key = fmt.upper()
        return self.formats.setdefault(key, FormatTotals())


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

class Reporter:
    def __init__(self, stream: Optional[TextIO] = None,
                 err: Optional[TextIO] = None,
                 width: Optional[int] = None,
                 tsv: bool = False,
                 use_title: Optional[bool] = None):
        self.out = stream or sys.stdout
        self.err = err or sys.stderr
        self.tsv = tsv
        self.name_w = NAME_W
        if width is None:
            width = _terminal_width()
        # Сужаем только колонку имени: числовые колонки трогать нельзя.
        if width < RULE_WIDTH:
            self.name_w = max(12, NAME_W - (RULE_WIDTH - width))
        # Строка данных — это ведущий пробел, имя, четыре разделителя и четыре
        # числовых колонки. Разделительная линия в 2.7 на один символ длиннее
        # строки данных (79 против 78), и это сохранено.
        self.row_width = 1 + self.name_w + 4 + ORIG_W + OPT_W + SAVE_W + PCT_W
        self.rule = "-" * (self.row_width + 1)
        self._is_tty = _isatty(self.out)
        self.use_title = self._is_tty if use_title is None else use_title
        self._last_title = ""

    # -- служебное ---------------------------------------------------------

    def line(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def decoration(self, text: str = "") -> None:
        """Оформление, которое не должно попадать в машинный вывод."""
        if not self.tsv:
            self.line(text)

    def eline(self, text: str = "") -> None:
        self.err.write(text + "\n")

    def flush(self) -> None:
        try:
            self.out.flush()
        except (OSError, ValueError):
            pass

    # -- шапка -------------------------------------------------------------

    def header(self) -> None:
        if self.tsv:
            self.line("status\tformat\tchain\tsource\tdestination"
                      "\toriginal\toptimized\tmessage")
            return
        self.line(self.rule)
        self.line(" " + pad_display("File Name", self.name_w) +
                  "| Original | Optimized |  Savings  | % Savings")
        self.line(" " + " " * self.name_w +
                  "| Size     | Size      |           |")
        self.line(self.rule)

    def tsv_row(self, *fields: str) -> None:
        """Записать строку машинного вывода с экранированием.

        Экранирование обязательно: в именах файлов на Linux встречаются и
        табуляция, и перевод строки. В 2.7 табуляция служила разделителем в
        `%filelist%` и «спасал» только белый список символов, который эти имена
        просто отбрасывал; теперь мы их принимаем, значит формат обязан их
        выдерживать.
        """
        self.line("\t".join(_escape_tsv(field) for field in fields))

    def missing_input_row(self, path: str) -> None:
        """Строка для входного пути, которого не существует."""
        if self.tsv:
            self.tsv_row("notfound", "", "", path, "", "0", "0",
                         "input path does not exist")

    def notes(self, messages: Iterable[str]) -> None:
        """Показать предупреждения о пропущенных шагах — один раз, до таблицы."""
        messages = list(messages)
        if not messages or self.tsv:
            return
        for message in messages:
            self.line(" ! " + message)
        self.line(self.rule)

    # -- строки ------------------------------------------------------------

    def row(self, result) -> None:
        if self.tsv:
            # Машинный вывод содержит строку на КАЖДЫЙ файл, включая неудачи:
            # молчаливое отсутствие строки — ровно та ошибка, из-за которой
            # пропавшие файлы в 2.7 было невозможно заметить.
            self.tsv_row(
                result.status, result.job.fmt, result.chain or "",
                str(result.job.src), str(result.dst or ""),
                str(result.orig_size), str(result.new_size), result.message or "",
            )
            return
        unit_size = pick_unit(result.orig_size)
        unit_delta = pick_unit(result.delta)
        name = crop_filename(os.path.basename(str(result.job.src)), self.name_w)
        self.line(" %s|%s|%s|%s|%s" % (
            pad_display(name, self.name_w),
            rjust_display(format_size(result.orig_size, unit_size), ORIG_W),
            rjust_display(format_size(result.new_size, unit_size), OPT_W),
            rjust_display(format_size(result.delta, unit_delta), SAVE_W),
            rjust_display(format_percent(result.delta, result.orig_size), PCT_W),
        ))

    def error_row(self, name: str, message: str) -> None:
        """Строка ошибки.

        В 2.7 сюда по недосмотру попадал числовой код корзины вместо текста
        (`:saverrorlog` вызывал `:printfileerr "%~f1" %~2`), так что причина
        пропуска пользователю не сообщалась.
        """
        if self.tsv:
            return
        # Сбрасываем stdout, иначе при перенаправлении строки ошибок всплывают
        # раньше таблицы: stderr не буферизуется, stdout буферизуется блоками.
        self.flush()
        # В таблице показывается базовое имя (`%~nx1` в 2.7), полный путь
        # печатается в группах ошибок в конце.
        self.eline(" %s|%s" % (
            pad_display(crop_filename(os.path.basename(name), ERR_NAME_W), ERR_NAME_W),
            _center(message, ERR_MSG_W),
        ))

    # -- итоги -------------------------------------------------------------

    def error_groups(self, summary: Summary) -> None:
        # Формулировки сохранены дословно из iCatalyst.bat:987 и :997 — это тот
        # же продукт, и пользователи узнают эти заголовки.
        if self.tsv:
            # Причина неудачи уже есть в колонке message каждой строки.
            return
        groups = (
            ("Images are not supported:", summary.unsupported),
            ("Images are not found:", summary.notfound),
        )
        if not any(items for _, items in groups) and not summary.failed:
            return
        self.line()
        self.line(_center("Error", len(self.rule)))
        for title, items in groups:
            if not items:
                continue
            self.line(self.rule)
            self.line()
            self.line(" " + title)
            for item in items:
                self.line("  " + item)
        if summary.failed:
            # Новая группа: в 2.7 причина неудачи вообще не сообщалась.
            self.line(self.rule)
            self.line()
            self.line(" Images failed to optimize:")
            for name, reason in summary.failed:
                self.line("  %s - %s" % (name, reason))
        self.line(self.rule)

    def totals(self, summary: Summary) -> None:
        if self.tsv:
            return
        shown = [key for key in FORMAT_ORDER
                 if key in summary.formats and summary.formats[key].total_files]
        if not shown:
            return
        self.line()
        self.line(_center("Total", len(self.rule)))
        self.line(self.rule)
        for key in shown:
            bucket = summary.formats[key]
            # Числитель — сколько файлов реально учтено, а не «всего минус
            # ошибки», как считал `:totalmsg` (`iCatalyst.bat:1178`). При
            # обычном прогоне это одно и то же, но после прерывания старая
            # формула заявляла [6/6] при нуле обработанных файлов.
            optimized = bucket.done_files
            if optimized <= 0:
                continue
            unit_size = pick_unit(bucket.original)
            unit_delta = pick_unit(bucket.delta)
            label = "%s [%d/%d]:" % (key, optimized, bucket.total_files)
            self.line(" %s|%s|%s|%s|%s" % (
                pad_display(label, self.name_w),
                rjust_display(format_size(bucket.original, unit_size), ORIG_W),
                rjust_display(format_size(bucket.optimized, unit_size), OPT_W),
                rjust_display(format_size(bucket.delta, unit_delta), SAVE_W),
                rjust_display(format_percent(bucket.delta, bucket.original), PCT_W),
            ))
        self.line(self.rule)

    def footer(self, outdir: Optional[str], started: str, finished: str) -> None:
        if self.tsv:
            return
        self.line()
        self.line(" Outdir: %s" % (outdir or "overwrite original images"))
        self.line()
        self.line(" Started  at - %s" % started)
        self.line(" Finished at - %s" % finished)
        self.line(self.rule)

    # -- заголовок окна ----------------------------------------------------

    def title(self, text: str) -> None:
        if not self.use_title or text == self._last_title:
            return
        self._last_title = text
        set_console_title(text)


def progress_title(progress: Dict[str, Tuple[str, int, int]],
                   suffix: str = "") -> str:
    """Собрать заголовок вида `[PNG Xtreme: 42% | JPG Baseline: 10%] Image Catalyst`.

    `progress` — формат → (метка режима, сделано, всего). Проценты усекаются,
    как это делал `set /a`.
    """
    parts = []
    for key in FORMAT_ORDER:
        item = progress.get(key)
        if not item:
            continue
        label, done, total = item
        percent = done * 100 // total if total else 0
        parts.append("%s %s: %d%%" % (key, label, percent))
    head = "[%s] " % " | ".join(parts) if parts else ""
    return "%s%s" % (head, suffix or APP_NAME)


def set_console_title(text: str) -> None:
    """Задать заголовок окна.

    На Windows — через SetConsoleTitleW напрямую, без запуска `cmd /c title`.
    На POSIX — управляющей последовательностью OSC, и только если это
    действительно терминал: иначе escape-байты попали бы в перенаправленный
    вывод.
    """
    if os.environ.get("ICATALYST_NO_TITLE"):
        return
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:
            pass
        return
    if os.environ.get("TERM", "") in ("", "dumb"):
        return
    stream = None
    if _isatty(sys.stdout):
        stream = sys.stdout
    else:
        try:
            stream = open("/dev/tty", "w")
        except OSError:
            return
    try:
        stream.write("\x1b]0;%s\x07" % text)
        stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        if stream is not sys.stdout:
            try:
                stream.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------

#: Экранирование для машинного вывода. Обратный слэш идёт первым, иначе
#: собственные escape-последовательности были бы экранированы повторно.
_TSV_ESCAPES = (("\\", "\\\\"), ("\t", "\\t"), ("\n", "\\n"), ("\r", "\\r"))


def _escape_tsv(text: str) -> str:
    for raw, escaped in _TSV_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def unescape_tsv(text: str) -> str:
    """Обратная операция к `_escape_tsv` — для потребителей `--tsv`."""
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            mapping = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}
            if nxt in mapping:
                out.append(mapping[nxt])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _center(text: str, width: int) -> str:
    pad = max(0, (width - display_width(text)) // 2)
    return " " * pad + text


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _terminal_width() -> int:
    try:
        import shutil
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def configure_streams() -> None:
    """Заставить stdout/stderr переживать любые имена файлов.

    Консоль Windows печатает Unicode через широкий API, но **перенаправленный**
    поток откатывается к кодовой странице локали (cp1251 или cp866) и падает с
    UnicodeEncodeError ровно на тех именах, которые мы починили. На Linux
    встречаются имена, не являющиеся корректным UTF-8, — их `os.fsdecode`
    отдаёт суррогатными парами.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass
