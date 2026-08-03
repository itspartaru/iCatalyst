"""Драйвер: прогоняет один файл через рецепт и записывает результат.

Здесь живут два решения, на которых держится вся переносимость.

**D1 — инструменты никогда не видят пользовательских путей.** Каждая задача
копирует исходник в короткий ASCII-файл во временном каталоге, гоняет цепочки
там и переносит результат на место. Это не косметика: DeflOpt 2007 года
получает аргументы через `GetCommandLineA` в ANSI-кодировке, и кириллица
ломается **внутри инструмента**, как бы аккуратно ни вёл себя Python. Тот же
приём разом снимает `& % ( ) ! ' "`, неразрывный пробел, эмодзи и MAX_PATH.

**D2 — ограничитель размера после каждого шага.** В 2.7 `:backup`/`:backup2`
срабатывали один раз в конце. Откат любого шага, чей результат не строго
меньше, делает любой необязательный шаг безопасным: добавление нового
инструмента не может ухудшить результат.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import imgcheck, recipes
from .paths import long_path
from .toolbox import Aborted, ProcessRegistry, ToolMissing

#: Статусы, в которых файл попадает в таблицу как обработанный.
STATUS_OK = "ok"
STATUS_KEPT = "kept"
#: Корзины ошибок из 2.7. «Bad characters» отсутствует намеренно: благодаря D1
#: она навсегда пуста, и её пустота — регрессионный тест на исходный баг.
STATUS_UNSUPPORTED = "unsupported"
STATUS_NOTFOUND = "notfound"
STATUS_FAILED = "failed"


class StepFailed(Exception):
    """Обязательный шаг цепочки не выполнился."""


@dataclass
class Job:
    src: Path
    #: Корень, из которого файл пришёл: нужен для зеркалирования дерева.
    root: Path
    fmt: str
    mode: int


@dataclass
class Ctx:
    src: Path
    fmt: str
    mode: int
    cfg: object
    tools: object
    work: Path
    out: Optional[Path] = None
    #: Сюда попадают параметры, добываемые из вывода инструментов
    #: (zc/zm/zs от TruePNG) и из самого файла (счётчик циклов GIF).
    scratch: Dict = field(default_factory=dict)


@dataclass
class Result:
    job: Job
    dst: Optional[Path]
    orig_size: int
    new_size: int
    status: str
    message: str = ""
    chain: str = ""

    @property
    def delta(self) -> int:
        """Изменение размера. Отрицательное значение — выигрыш.

        Знак сохранён как в 2.7: `set /a "change=%~z1-%~2"`, где %~z1 — новый
        размер, а %~2 — исходный (`iCatalyst.bat:898`).
        """
        return self.new_size - self.orig_size


class Runner:
    def __init__(self, cfg, tools, mapper, tmpdir: Path,
                 verify: bool = False, strict_lossless: bool = False,
                 progress=None):
        self.cfg = cfg
        self.tools = tools
        self.mapper = mapper
        self.tmpdir = Path(tmpdir)
        self.verify = verify
        self.strict_lossless = strict_lossless
        self.progress = progress
        #: Через реестр идут все вызовы инструментов, чтобы по Ctrl-C их можно
        #: было прибить, а не ждать завершения.
        self.processes = ProcessRegistry()
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._recipe_lock = threading.Lock()
        self._recipes: Dict = {}

    # -- временные файлы ---------------------------------------------------

    def _temp(self, suffix: str) -> Path:
        """Короткое ASCII-имя во временном каталоге — сердце решения D1."""
        with self._lock:
            index = next(self._counter)
        return self.tmpdir / ("%06d%s" % (index, suffix))

    # -- рецепты -----------------------------------------------------------

    def recipe(self, fmt: str, mode: int):
        key = (fmt, mode)
        with self._recipe_lock:
            return self._recipe_locked(key, fmt, mode)

    def _recipe_locked(self, key, fmt: str, mode: int):
        if key not in self._recipes:
            recipe = recipes.build(fmt, mode, self.cfg)
            chains = recipes.runnable_chains(recipe, self.tools) if recipe else []
            if recipe is not None and not chains:
                # На Windows без TruePNG имеет смысл откатиться к POSIX-цепочке,
                # чтобы формат не отваливался целиком.
                spare = recipes.fallback_recipe(fmt, mode, self.cfg)
                if spare is not None and recipes.runnable_chains(spare, self.tools):
                    recipe = spare
                    chains = recipes.runnable_chains(spare, self.tools)
            self._recipes[key] = (recipe, chains)
        return self._recipes[key]

    def missing_tools_message(self, recipe) -> str:
        names = []
        for chain in recipe.chains:
            for name in chain.requires:
                if self.tools.find(name) is None and name not in names:
                    names.append(name)
        try:
            self.tools.require(names[0])
        except ToolMissing as exc:
            return str(exc)
        except IndexError:
            pass
        return "нет инструментов для режима %s %s" % (recipe.fmt.upper(), recipe.label)

    # -- выполнение --------------------------------------------------------

    def _run_step(self, ctx: Ctx, step) -> None:
        tool = self.tools.find(step.tool)
        if tool is None:
            if not step.optional:
                raise StepFailed("инструмент %s недоступен" % step.tool)
            self.tools.warn_once(
                "missing:%s" % step.tool,
                "шаг %s пропущен: инструмент %s не найден" % (step.name, step.tool),
            )
            return

        if step.produces == "out":
            ctx.out = self._temp(ctx.work.suffix)
            backup = None
        else:
            ctx.out = None
            # Инструмент правит файл на месте, поэтому нужна копия для отката.
            backup = self._temp(ctx.work.suffix)
            shutil.copyfile(long_path(ctx.work), long_path(backup))

        argv = step.argv(ctx)
        if argv is None:
            return

        try:
            proc = self.processes.run([str(tool.path), *argv],
                                      timeout=self.cfg.timeout,
                                      capture=step.parse is not None)
        except subprocess.TimeoutExpired:
            if backup is not None:
                shutil.copyfile(long_path(backup), long_path(ctx.work))
            message = "шаг %s не завершился за %g с" % (step.name, self.cfg.timeout)
            if step.optional:
                self.tools.warn_once("timeout:%s" % step.name, message)
                return
            raise StepFailed(message)
        except Aborted:
            if backup is not None:
                shutil.copyfile(long_path(backup), long_path(ctx.work))
            raise
        except OSError as exc:
            if step.optional:
                self.tools.warn_once("oserror:%s" % step.name, str(exc))
                return
            raise StepFailed("шаг %s: %s" % (step.name, exc))

        if proc.returncode not in step.ok_codes:
            message = "шаг %s завершился с кодом %d" % (step.name, proc.returncode)
            if backup is not None:
                shutil.copyfile(long_path(backup), long_path(ctx.work))
            if step.optional:
                self.tools.warn_once("exit:%s" % step.name, message)
                return
            raise StepFailed(message)

        if step.parse is not None:
            step.parse(ctx, proc)

        # --- D2: ограничитель размера ------------------------------------
        if step.produces == "out":
            if ctx.out is None or not os.path.exists(long_path(ctx.out)):
                if not step.optional:
                    raise StepFailed("шаг %s не создал файл" % step.name)
                return
            new_size = os.path.getsize(long_path(ctx.out))
            if new_size == 0:
                # Нулевая длина при нулевом коде возврата — сломанный
                # инструмент, а не «не удалось сжать». Молча продолжать нельзя:
                # именно так теряются файлы.
                message = "шаг %s вернул файл нулевой длины" % step.name
                if step.optional:
                    self.tools.warn_once("empty:%s" % step.name, message)
                    return
                raise StepFailed(message)
            if new_size < os.path.getsize(long_path(ctx.work)):
                ctx.work = ctx.out
            # Иначе результат отбрасывается, и цепочка идёт дальше с прежним
            # файлом: это и есть ограничитель D2.
        else:
            size = os.path.getsize(long_path(ctx.work))
            if size == 0 or size >= os.path.getsize(long_path(backup)):
                shutil.copyfile(long_path(backup), long_path(ctx.work))

    def _run_chain(self, chain, seed: Path, job: Job, scratch: Dict) -> Optional[Path]:
        work = self._temp(seed.suffix)
        shutil.copyfile(long_path(seed), long_path(work))
        ctx = Ctx(src=job.src, fmt=job.fmt, mode=job.mode, cfg=self.cfg,
                  tools=self.tools, work=work, scratch=dict(scratch))
        for step in chain.steps:
            self._run_step(ctx, step)
        return ctx.work

    # -- основной вход -----------------------------------------------------

    def process(self, job: Job) -> Result:
        try:
            return self._process(job)
        finally:
            # Прогресс двигает рабочий поток, а не печать строки: иначе процент
            # в заголовке замирал бы, пока таблица ждёт медленный файл.
            if self.progress is not None:
                self.progress.bump(job.fmt)

    def _process(self, job: Job) -> Result:
        src = job.src
        try:
            orig_size = os.path.getsize(long_path(src))
        except OSError:
            return Result(job, None, 0, 0, STATUS_NOTFOUND, "файл недоступен")
        if orig_size == 0:
            return Result(job, None, 0, 0, STATUS_UNSUPPORTED, "файл пуст")

        with open(long_path(src), "rb") as fh:
            src_data = fh.read()

        actual = imgcheck.sniff(src_data)
        if actual is None:
            return Result(job, None, orig_size, orig_size, STATUS_UNSUPPORTED,
                          "формат не распознан")
        if actual != job.fmt:
            # Расширение врёт. В 2.7 такой файл уходил в цепочку по расширению
            # и падал уже внутри инструмента.
            return Result(job, None, orig_size, orig_size, STATUS_UNSUPPORTED,
                          "расширение обещает %s, содержимое — %s" % (job.fmt, actual))
        try:
            imgcheck.validate(long_path(src), actual)
        except imgcheck.ImageError as exc:
            return Result(job, None, orig_size, orig_size, STATUS_UNSUPPORTED, str(exc))

        recipe, chains = self.recipe(job.fmt, job.mode)
        if recipe is None:
            return Result(job, None, orig_size, orig_size, STATUS_UNSUPPORTED,
                          "режим %d для %s не поддерживается" % (job.mode, job.fmt))
        if not chains:
            return Result(job, None, orig_size, orig_size, STATUS_FAILED,
                          self.missing_tools_message(recipe))

        scratch = self._prepare_scratch(job, src_data)

        seed = self._temp(_extension_for(job.fmt))
        shutil.copyfile(long_path(src), long_path(seed))

        best: Optional[Path] = None
        best_size = orig_size
        best_chain = ""
        failures: List[str] = []
        # Считаем цепочки, доехавшие до конца с пригодным файлом, даже если
        # выигрыша не дали: без этого «уже оптимизировано» путалось бы с ошибкой.
        succeeded = 0
        for chain in chains:
            try:
                candidate = self._run_chain(chain, seed, job, scratch)
            except StepFailed as exc:
                failures.append("%s: %s" % (chain.name, exc))
                continue
            if candidate is None or not os.path.exists(long_path(candidate)):
                failures.append("%s: результат не создан" % chain.name)
                continue
            size = os.path.getsize(long_path(candidate))
            if size == 0:
                failures.append("%s: результат нулевой длины" % chain.name)
                continue
            try:
                with open(long_path(candidate), "rb") as fh:
                    cand_data = fh.read()
                imgcheck.validate_data(cand_data, job.fmt)
                problem = self._check_lossless(src_data, cand_data, job, recipe)
            except imgcheck.ImageError as exc:
                failures.append("%s: %s" % (chain.name, exc))
                continue
            if problem:
                failures.append("%s: %s" % (chain.name, problem))
                continue
            succeeded += 1
            if size < best_size:
                best, best_size, best_chain = candidate, size, chain.name

        if not succeeded:
            return Result(job, None, orig_size, orig_size, STATUS_FAILED,
                          "; ".join(failures) or "ни одна цепочка не дала результата")

        dst = self.mapper.destination(src, job.root)
        if best is None:
            # Выигрыша нет. Оригинал сохраняется; при выводе в другой каталог
            # он всё равно копируется, как это делал `:backup2`.
            if dst != src:
                self._commit(seed, dst, src)
            return Result(job, dst, orig_size, orig_size, STATUS_KEPT,
                          "; ".join(failures))

        self._commit(best, dst, src)
        return Result(job, dst, orig_size, best_size, STATUS_OK,
                      "; ".join(failures), best_chain)

    # -- вспомогательное ---------------------------------------------------

    def _prepare_scratch(self, job: Job, src_data: bytes) -> Dict:
        scratch: Dict = {}
        if job.fmt == "gif":
            try:
                info = imgcheck.read_gif(src_data, with_frames=False)
                scratch["loop_count"] = info.loop_count
            except imgcheck.ImageError:
                scratch["loop_count"] = None
        elif job.fmt == "jpg" and job.mode == 3:
            try:
                scratch["orig_progressive"] = imgcheck.read_jpeg(src_data).is_progressive
            except imgcheck.ImageError:
                scratch["orig_progressive"] = False
        return scratch

    def _check_lossless(self, src_data: bytes, dst_data: bytes,
                        job: Job, recipe) -> Optional[str]:
        stripped = {
            "png": self.cfg.pngtags,
            "jpg": self.cfg.jpegtags,
            "gif": self.cfg.giftags,
        }[job.fmt] != "none"
        problem = imgcheck.structure_equal(src_data, dst_data, job.fmt, stripped=stripped)
        if problem:
            return problem
        # GIF сравнивается по пикселям всегда: файлы маленькие, а
        # `--optimize=3` перекраивает кадры так, что ошибка была бы видна.
        if job.fmt == "gif" or (job.fmt == "png" and self.verify):
            allow_dirty = (recipe.lossless_class == "visible-exact"
                           and not self.strict_lossless)
            return imgcheck.pixels_equal(src_data, dst_data, job.fmt,
                                         allow_dirty_transparent=allow_dirty)
        return None

    def _commit(self, produced: Path, dst: Path, src: Path) -> None:
        """Атомарно поставить результат на место.

        Запись идёт во временный файл рядом с назначением, затем `os.replace`.
        Это лучше, чем `copy /b /y` из 2.7: тот при обрыве оставлял обрезанный
        файл на месте оригинала.
        """
        dst_dir = dst.parent
        os.makedirs(long_path(dst_dir), exist_ok=True)
        staging = dst_dir / (".icatalyst-%s.tmp" % os.path.basename(str(produced)))
        shutil.copyfile(long_path(produced), long_path(staging))
        if self.cfg.preserve_mtime:
            try:
                shutil.copystat(long_path(src), long_path(staging))
            except OSError:
                pass
        os.replace(long_path(staging), long_path(dst))


def _extension_for(fmt: str) -> str:
    return {"png": ".png", "jpg": ".jpg", "gif": ".gif"}[fmt]
