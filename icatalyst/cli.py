"""Разбор аргументов и сценарий прогона.

Ключи `/png:#`, `/jpg:#`, `/gif:#` и `/outdir:#` сохранены полностью: их
разбирал `Tools/scripts/pfilter.js`, и они описаны в README, в ярлыках и в
привычках пользователей. Дополнительные возможности добавлены в стиле `--flag`,
чтобы не смешиваться с legacy-синтаксисом.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import APP_NAME, __version__
from . import config as cfgmod
from . import picker, recipes, report, scan, ui
from .paths import OutputMapper
from .pipeline import (STATUS_FAILED, STATUS_KEPT, STATUS_NOTFOUND, STATUS_OK,
                       STATUS_UNSUPPORTED, Job, Runner)
from .toolbox import Toolbox

#: Соответствие ключа командной строки и формата.
_MODE_KEYS = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif"}

_MAX_MODE = {"png": 2, "jpg": 3, "gif": 1}


class UsageError(Exception):
    """Некорректные аргументы командной строки."""


@dataclass
class Options:
    modes: Dict[str, int] = field(default_factory=dict)
    outdir: Optional[str] = None
    inputs: List[str] = field(default_factory=list)
    config: Optional[str] = None
    verify: bool = False
    strict_lossless: bool = False
    tsv: bool = False
    doctor: bool = False
    width: Optional[int] = None
    threads: Optional[int] = None
    picker: Optional[str] = None
    dry_run: bool = False
    stream: bool = False
    pause: Optional[bool] = None
    show_help: bool = False
    show_version: bool = False


def parse_args(argv: Sequence[str]) -> Options:
    opts = Options()
    pending: Optional[str] = None
    for raw in argv:
        if pending is not None:
            _apply_value(opts, pending, raw)
            pending = None
            continue

        if raw in ("-h", "--help", "/?", "/help"):
            opts.show_help = True
            continue
        if raw in ("--version", "/version"):
            opts.show_version = True
            continue
        if raw in ("--verify",):
            opts.verify = True
            continue
        if raw in ("--strict-lossless",):
            opts.strict_lossless = True
            continue
        if raw in ("--tsv",):
            opts.tsv = True
            continue
        if raw in ("--doctor", "--check-tools"):
            opts.doctor = True
            continue
        if raw in ("--dry-run",):
            opts.dry_run = True
            continue
        if raw in ("--stream",):
            opts.stream = True
            continue
        if raw in ("--no-pause",):
            opts.pause = False
            continue
        if raw in ("--pause",):
            opts.pause = True
            continue
        if raw in ("--config", "--width", "--threads", "--picker"):
            pending = raw.lstrip("-")
            continue
        if raw.startswith("--") and "=" in raw:
            key, _, value = raw[2:].partition("=")
            _apply_value(opts, key, value)
            continue

        # Legacy-форма: /png:2, /outdir:C:\temp — регистр не важен.
        if (raw.startswith("/") or raw.startswith("-")) and ":" in raw:
            key, _, value = raw[1:].partition(":")
            key = key.lower()
            if key in _MODE_KEYS or key == "outdir":
                _apply_value(opts, key, value)
                continue
            raise UsageError("неизвестный параметр: %s" % raw)

        opts.inputs.append(raw)
    if pending is not None:
        raise UsageError("параметр --%s требует значения" % pending)
    return opts


def _apply_value(opts: Options, key: str, value: str) -> None:
    key = key.lower()
    if key in _MODE_KEYS:
        fmt = _MODE_KEYS[key]
        try:
            mode = int(value)
        except ValueError:
            raise UsageError("параметр /%s: ожидалось число, получено %r" % (key, value))
        if not 0 <= mode <= _MAX_MODE[fmt]:
            raise UsageError("параметр /%s: допустимы значения 0..%d"
                             % (key, _MAX_MODE[fmt]))
        opts.modes[fmt] = mode
        return
    if key == "outdir":
        opts.outdir = value
        return
    if key == "config":
        opts.config = value
        return
    if key == "width":
        try:
            opts.width = max(40, int(value))
        except ValueError:
            raise UsageError("параметр --width: ожидалось число")
        return
    if key == "threads":
        try:
            opts.threads = max(1, int(value))
        except ValueError:
            raise UsageError("параметр --threads: ожидалось число")
        return
    if key == "picker":
        allowed = ("auto", "tk", "zenity", "kdialog", "osascript", "terminal", "none")
        if value.lower() not in allowed:
            raise UsageError("параметр --picker: допустимо %s" % ", ".join(allowed))
        opts.picker = value.lower()
        return
    raise UsageError("неизвестный параметр: --%s" % key)


# ---------------------------------------------------------------------------

def _doctor(cfg, tools, reporter: report.Reporter) -> int:
    """Показать найденные инструменты и точные команды для этой машины.

    Это и есть механизм документации: программа сама рассказывает, что
    выполнит, поэтому README не может разойтись с реальностью.
    """
    for line in tools.report():
        reporter.line(line)
    reporter.line()
    reporter.line("Режимы на этой машине")
    reporter.line()

    class _Stub:
        def __init__(self, fmt, mode):
            self.fmt = fmt
            self.mode = mode
            self.cfg = cfg
            self.tools = tools
            self.work = Path("IN" + {"png": ".png", "jpg": ".jpg", "gif": ".gif"}[fmt])
            self.out = Path("OUT" + self.work.suffix)
            self.src = self.work
            self.scratch = {"loop_count": 0, "orig_progressive": False}

    for fmt in ("png", "jpg", "gif"):
        for mode in range(1, _MAX_MODE[fmt] + 1):
            recipe = recipes.build(fmt, mode, cfg)
            if recipe is None:
                continue
            chains = recipes.runnable_chains(recipe, tools)
            head = "  %s %s" % (fmt.upper(), recipe.label)
            if not chains:
                reporter.line("%s — недоступен (нет инструментов)" % head)
                continue
            extra = " [%s]" % recipe.note if recipe.note else ""
            reporter.line("%s — %s, %s%s"
                          % (head, recipe.lossless_class,
                             "гонка %d цепочек" % len(chains) if len(chains) > 1
                             else "одна цепочка", extra))
            for chain in chains:
                ctx = _Stub(fmt, mode)
                for step in chain.steps:
                    if step.func is not None:
                        reporter.line("      (внутри программы) %s" % step.name)
                        continue
                    tool = tools.find(step.tool)
                    if tool is None:
                        continue
                    if step.produces == "out":
                        ctx.out = Path("OUT" + ctx.work.suffix)
                    argv = step.argv(ctx)
                    if argv is None:
                        continue
                    reporter.line("      %s %s" % (step.tool, " ".join(argv)))
    return 0


def _resolve_modes(opts: Options, found: scan.ScanResult, cfg,
                   tools, reporter: report.Reporter) -> Dict[str, int]:
    modes: Dict[str, int] = {}
    for fmt in ("png", "jpg", "gif"):
        if fmt not in found.present:
            modes[fmt] = 0
            continue
        if fmt in opts.modes:
            modes[fmt] = opts.modes[fmt]
            continue
        if not ui.is_interactive():
            raise UsageError(
                "во входных данных есть %s, но режим не задан: укажите /%s:#"
                % (fmt.upper(), fmt)
            )
        # Подсказка про то, чем реально будет сжиматься Xtreme на этой
        # платформе: метка в заголовке окна остаётся прежней, но в меню честно
        # написано, какие инструменты за ней стоят.
        note = None
        if fmt == "png":
            recipe = recipes.build("png", 2, cfg)
            if recipe is not None and recipe.note:
                note = recipe.note
        ui.clear_screen()
        modes[fmt] = ui.ask_mode(fmt, note)
    return modes


def _resolve_outdir(opts: Options, cfg, tools) -> Optional[Path]:
    """Куда писать результаты. None означает «перезаписать оригиналы».

    Отмена в диалоге — это осмысленный ответ «перезаписать оригиналы», ровно как
    Cancel в `browsefolder.exe` (`iCatalyst.bat:163`). А вот невозможность
    спросить — не ответ, и молча уничтожать оригиналы в этом случае нельзя.
    """
    raw = opts.outdir if opts.outdir is not None else cfg.outdir
    value = (raw or "").strip()
    if value.lower() == "false":
        return None
    if value.lower() in ("true", ""):
        result = picker.pick_directory(cfg.picker)
        if result.note:
            tools.warn_once("picker", result.note)
        if result.choice is picker.Choice.ABORT:
            raise UsageError(result.note or "выбор каталога прерван")
        if result.choice is picker.Choice.IN_PLACE:
            return None
        value = str(result.path)
    path = Path(os.path.abspath(os.path.expanduser(value)))
    os.makedirs(path, exist_ok=True)
    return path


def run(argv: Sequence[str]) -> int:
    report.configure_streams()
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    if opts.show_version:
        print("%s %s" % (APP_NAME, __version__))
        return 0

    reporter = report.Reporter(width=opts.width, tsv=opts.tsv)

    if opts.show_help or (not opts.inputs and not opts.doctor):
        ui.banner(ui.help_text(__version__))
        ui.pause(ui.should_pause() if opts.pause is None else opts.pause)
        return 0

    try:
        cfg = cfgmod.load(opts.config)
    except cfgmod.ConfigError as exc:
        sys.stderr.write("Ошибка в файле конфигурации: %s\n" % exc)
        return 2
    if opts.threads:
        cfg.thread = opts.threads
    if opts.picker:
        cfg.picker = opts.picker
    if opts.strict_lossless:
        # Флаг обязан менять саму команду, а не только последующую проверку:
        # иначе `--lossy_transparent` всё равно уезжает в zopflipng (а на
        # Windows `/a1` — в TruePNG), и обещание «не менять RGB под полностью
        # прозрачными пикселями» не выполняется.
        for mode_opts in (cfg.png_advanced, cfg.png_xtreme):
            mode_opts.dirty_transparency = False
            mode_opts.legacy_flags = cfgmod.render_truepng_flags(mode_opts)

    tools = Toolbox(cfg)

    if opts.doctor:
        return _doctor(cfg, tools, reporter)

    found = scan.scan(opts.inputs)
    if not found.files:
        sys.stderr.write(" No images found. Please check input and try again.\n")
        return 1

    try:
        modes = _resolve_modes(opts, found, cfg, tools, reporter)
    except UsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    jobs = [Job(src=src, root=root, fmt=fmt, mode=modes[fmt])
            for src, root, fmt in found.files if modes.get(fmt)]
    if not jobs:
        sys.stderr.write(" Nothing to do: all formats are set to Skip.\n")
        return 0

    try:
        outdir = _resolve_outdir(opts, cfg, tools)
    except UsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    mapper = OutputMapper(outdir)

    summary = report.Summary()
    for _, _, fmt in found.files:
        if modes.get(fmt):
            summary.bucket(fmt).total_files += 1
    for fmt, mode in modes.items():
        if mode:
            recipe = recipes.build(fmt, mode, cfg)
            if recipe is not None:
                summary.bucket(fmt).label = recipe.label

    for name in found.missing:
        summary.notfound.append(name)

    started = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    tmpdir = Path(tempfile.mkdtemp(prefix="icatalyst-"))
    exit_code = 0
    interrupted = False
    progress = report.Progress()
    # thread=0 означает «по числу процессоров» — как %NUMBER_OF_PROCESSORS% в 2.7.
    workers = max(1, min(32, cfg.thread or os.cpu_count() or 1))
    try:
        runner = Runner(cfg, tools, mapper, tmpdir,
                        verify=opts.verify, strict_lossless=opts.strict_lossless,
                        progress=progress)
        if opts.dry_run:
            for job in jobs:
                reporter.line("%s -> %s" % (job.src, mapper.destination(job.src, job.root)))
            return 0

        reporter.title(report.progress_title({}, APP_NAME))
        # Предупреждения о недоступных шагах печатаются один раз, до таблицы:
        # рецепты строятся заранее, поэтому к этому моменту они уже известны.
        for fmt, mode in modes.items():
            if mode:
                runner.recipe(fmt, mode)
        reporter.notes(tools.notes())
        reporter.header()
        # Несуществующие входные пути обязаны попасть и в машинный вывод, а не
        # только в текстовую группу ошибок в конце.
        for name in found.missing:
            reporter.missing_input_row(name)
        # Сброс сразу после шапки. Без него при перенаправленном выводе шапка
        # лежит в буфере до завершения первого файла, а `optipng -o7` на большом
        # PNG считается минутами: пользователь всё это время видит пустой экран.
        reporter.flush()

        for fmt, mode in modes.items():
            if mode:
                bucket = summary.bucket(fmt)
                progress.add(fmt, bucket.label, bucket.total_files)
        interrupted = _drive(jobs, runner, reporter, summary, progress,
                             workers, opts.stream)
        reporter.decoration(reporter.rule)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    finished = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    reporter.error_groups(summary)
    reporter.totals(summary)
    reporter.footer(str(outdir) if outdir else None, started, finished)
    reporter.title(APP_NAME)
    if interrupted:
        sys.stderr.write(" Interrupted by user.\n")
        exit_code = 130
    elif summary.failed:
        exit_code = 1
    ui.pause(ui.should_pause() if opts.pause is None else opts.pause)
    return exit_code


def _account(summary: report.Summary, result, reporter: report.Reporter) -> None:
    bucket = summary.bucket(result.job.fmt)
    name = str(result.job.src)
    if result.status in (STATUS_OK, STATUS_KEPT):
        bucket.done_files += 1
        bucket.original += result.orig_size
        bucket.optimized += result.new_size
    else:
        bucket.errors += 1
        if result.status == STATUS_NOTFOUND:
            summary.notfound.append(name)
        elif result.status == STATUS_UNSUPPORTED:
            summary.unsupported.append(name)
        else:
            summary.failed.append((name, result.message))

    if reporter.tsv:
        # В машинном выводе строка есть для любого исхода.
        reporter.row(result)
        return
    if result.status in (STATUS_OK, STATUS_KEPT):
        reporter.row(result)
    elif result.status == STATUS_NOTFOUND:
        reporter.error_row(name, "not found")
    elif result.status == STATUS_UNSUPPORTED:
        reporter.error_row(name, result.message or "not supported")
    else:
        reporter.error_row(name, "failed")


def _drive(jobs, runner, reporter: report.Reporter, summary: report.Summary,
           progress: report.Progress, workers: int, stream: bool) -> bool:
    """Прогнать задачи через пул. Вернуть True, если прервано пользователем.

    Потоки, а не процессы: рабочий почти всё время ждёт `subprocess`, так что GIL
    не мешает, а счётчики и временный каталог разделяются без лишних хлопот.

    Вывод по умолчанию идёт **в порядке входных данных**, а не завершения. В 2.7
    порядок определялся файлами логов на поток и был произвольным — это был
    артефакт реализации, а не свойство. Детерминированный вывод делает отчёт
    проверяемым эталоном и пригодным для diff.
    """
    if workers <= 1:
        for job in jobs:
            try:
                result = runner.process(job)
            except KeyboardInterrupt:
                runner.processes.abort()
                return True
            _account(summary, result, reporter)
            reporter.title(report.progress_title(progress.snapshot(), APP_NAME))
            reporter.flush()
        return False

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                 thread_name_prefix="icatalyst")
    futures = [pool.submit(runner.process, job) for job in jobs]
    try:
        sequence = (concurrent.futures.as_completed(futures) if stream else futures)
        for future in sequence:
            while True:
                try:
                    result = future.result(timeout=0.25)
                    break
                except concurrent.futures.TimeoutError:
                    # Пока ждём свою очередь на медленном файле, проценты в
                    # заголовке всё равно двигаются: это и заменяет отдельный
                    # поток обновления интерфейса.
                    reporter.title(report.progress_title(progress.snapshot(),
                                                         APP_NAME))
            _account(summary, result, reporter)
            reporter.title(report.progress_title(progress.snapshot(), APP_NAME))
            reporter.flush()
    except KeyboardInterrupt:
        runner.processes.abort()
        pool.shutdown(wait=False, cancel_futures=True)
        return True
    finally:
        pool.shutdown(wait=True)
    return False
