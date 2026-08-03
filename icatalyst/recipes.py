"""Цепочки оптимизации, описанные данными.

Один рецепт — это формат, режим и несколько **независимых цепочек**, которые
считаются наперегонки; побеждает та, что дала файл меньше. Идиома взята из
самого проекта: GIF всегда считался и с `--optimize=0`, и с `--optimize=3`, а
`:backup2` отказывался принимать результат, который не строго меньше входа.
Распространение этого на PNG превращает вопрос «а замена не хуже TruePNG?» из
спора в гарантию: результат не больше ни одного из кандидатов и не больше входа.

Деградация двухуровневая. Цепочка, чьи обязательные инструменты не нашлись,
просто не участвует в гонке. Шаг с `optional=True` пропускается с **одним**
предупреждением на весь прогон — печатать его на каждый файл значило бы
похоронить таблицу.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from . import config as cfgmod

#: Уровень optipng для режима Advanced. Режимы определены семантически:
#: Advanced — структурная оптимизация и один проход deflate (секунды),
#: Xtreme — исчерпывающий поиск плюс zopfli (в 5–15 раз медленнее).
OPTIPNG_ADVANCED = "-o5"
OPTIPNG_XTREME = "-o7"


@dataclass(frozen=True)
class Step:
    name: str
    #: Логическое имя инструмента для Toolbox.
    tool: str
    #: Собирает аргументы. Возвращает None, если шаг не нужен при этой
    #: конфигурации (например, удаление метаданных выключено).
    argv: Callable
    #: `inplace` — инструмент правит ctx.work на месте;
    #: `out` — пишет в ctx.out, и драйвер решает, принимать ли результат.
    produces: str = "inplace"
    #: Отсутствие инструмента или ненулевой код не роняют файл.
    optional: bool = False
    #: Разбор вывода инструмента (нужен для скрейпа параметров TruePNG).
    parse: Optional[Callable] = None
    ok_codes: Tuple[int, ...] = (0,)


@dataclass(frozen=True)
class Chain:
    name: str
    steps: Tuple[Step, ...]
    #: Без этих инструментов цепочка не участвует.
    requires: Tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Recipe:
    fmt: str
    mode: int
    #: Метка для заголовка окна: `PNG Xtreme: 42%`. Совпадает с 2.7 дословно.
    label: str
    chains: Tuple[Chain, ...]
    note: str = ""
    #: `bit-exact` или `visible-exact`. Второе означает, что разрешено менять
    #: RGB под полностью прозрачными пикселями (`/a1` в config.ini).
    lossless_class: str = "bit-exact"


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _optipng_args(ctx, level: str) -> list:
    opts = ctx.cfg.png_mode(ctx.mode)
    args = ["-quiet", level]
    if ctx.cfg.pngtags == cfgmod.STRIP_ALL:
        args += ["-strip", "all"]
    # optipng не умеет выборочно сохранять ICC, поэтому keep-icc означает
    # «не удалять ничего»; о расхождении сообщаем один раз.
    elif ctx.cfg.pngtags == cfgmod.STRIP_KEEP_ICC:
        ctx.tools.warn_once(
            "optipng-keep-icc",
            "optipng не умеет выборочно удалять метаданные: при pngtags=keep-icc "
            "сохраняются все чанки, а не только ICC",
        )
    if opts.keep_colortype:
        args.append("-nc")
    if opts.keep_bitdepth:
        args.append("-nb")
    if opts.keep_palette:
        args.append("-np")
    args += ["-out", str(ctx.out), str(ctx.work)]
    return args


def _zopflipng_args(ctx, lossy_transparent: bool = False) -> Optional[list]:
    opts = ctx.cfg.png_mode(ctx.mode)
    if lossy_transparent and not opts.dirty_transparency:
        return None
    args = ["-y", "--iterations=%d" % ctx.cfg.xtreme_iterations, "--filters=0me"]
    if lossy_transparent:
        # Прямое соответствие TruePNG `/a1`: переписать RGB под полностью
        # прозрачными пикселями. Визуально идентично, побитово — нет, и далеко
        # не всегда меньше, поэтому это отдельный шаг под ограничителем размера.
        args.append("--lossy_transparent")
    if ctx.cfg.pngtags != cfgmod.STRIP_ALL:
        # zopflipng по умолчанию выбрасывает необязательные чанки, поэтому,
        # если удаление метаданных не запрошено, их надо перечислить явно.
        args.append("--keepchunks=tRNS,gAMA,cHRM,sRGB,iCCP,bKGD,sBIT,pHYs,tEXt,iTXt,zTXt")
    args += [str(ctx.work), str(ctx.out)]
    return args


def _oxipng_args(ctx, level: str, zopfli: bool, alpha: bool) -> Optional[list]:
    """Аргументы oxipng.

    Набор нарочно минимальный: `-o`, `--strip`, `--out` и `-t` стабильны от
    версии к версии, а вот синтаксис `-f`/`--filters` между мажорными версиями
    менялся, поэтому фильтры не задаются вовсе. Всё остальное добавляется только
    при наличии соответствующей возможности, определённой зондом.
    """
    tool = ctx.tools.find("oxipng")
    opts = ctx.cfg.png_mode(ctx.mode)
    if alpha and not (opts.dirty_transparency and tool is not None
                      and tool.has("alpha")):
        return None
    args = ["--quiet", "-o", level, "-t", "1"]
    if zopfli and tool is not None and tool.has("zopfli"):
        args.append("-Z")
    if tool is not None and tool.has("strip"):
        args += ["--strip", "all" if ctx.cfg.pngtags == cfgmod.STRIP_ALL else "safe"]
    if alpha:
        args.append("-a")
    if opts.keep_colortype:
        args.append("--nc")
    if opts.keep_bitdepth:
        args.append("--nb")
    if opts.keep_palette:
        args.append("--np")
    args += ["--out", str(ctx.out), str(ctx.work)]
    return args


def _oxipng_chain(mode: int, suffix: str = "") -> Chain:
    """Цепочка oxipng: обычный проход и, отдельным шагом, «грязная прозрачность».

    Второй шаг добавлен именно шагом, а не заменой флага, потому что `-a` вовсе
    не всегда выигрывает: на изображении, где RGB под полностью прозрачными
    пикселями продолжает плавный градиент видимой части, обнуление этих пикселей
    рвёт градиент и раздувает файл (измерено: 205 → 1385 байт). Ограничитель
    размера отбросит такой результат, так что шаг может только помочь.
    """
    level = "4" if mode == 1 else "max"
    zopfli = mode == 2
    return Chain(
        "oxipng" + suffix,
        (Step("oxipng", "oxipng",
              lambda ctx: _oxipng_args(ctx, level, zopfli, alpha=False),
              produces="out"),
         Step("oxipng-alpha", "oxipng",
              lambda ctx: _oxipng_args(ctx, level, zopfli, alpha=True),
              produces="out", optional=True)),
        ("oxipng",),
    )


#: `-z` — режим пересжатия, `-2`/`-4` — уровень. Уровни advancecomp:
#: 1=zlib, 2=libdeflate, 3=7z, 4=zopfli, то есть `-2` даёт ровно то же, что
#: вложенный в Windows advdef 2.0. В 2.7 флаги были склеены в `-z2`: getopt
#: разбирает это как две короткие опции, так что формы эквивалентны, но явная не
#: оставляет места догадкам.
def _advdef_step(name: str, level: str) -> Step:
    return Step(name, "advdef",
                lambda ctx: ["-z", level, "-q", str(ctx.work)], optional=True)


def _optipng_step(level: str) -> Step:
    return Step("optipng", "optipng", lambda ctx: _optipng_args(ctx, level),
                produces="out")


def _posix_advanced_chains(suffix: str = "") -> tuple:
    """Быстрые цепочки: структурная оптимизация и один проход deflate."""
    return (
        Chain("optipng+advdef" + suffix,
              (_optipng_step(OPTIPNG_ADVANCED), _advdef_step("advdef", "-2")),
              ("optipng",)),
        _oxipng_chain(1, suffix),
    )


def _png_posix(mode: int, cfg) -> Recipe:
    opts = cfg.png_mode(mode)
    if mode == 1:
        chains = _posix_advanced_chains()
        note = ("optipng %s + advdef -z2 (TruePNG и DeflOpt существуют только "
                "под Windows)" % OPTIPNG_ADVANCED)
    else:
        optipng = _optipng_step(OPTIPNG_XTREME)
        chains = (
            Chain("optipng+zopflipng",
                  (optipng,
                   Step("zopflipng", "zopflipng",
                        lambda ctx: _zopflipng_args(ctx, lossy_transparent=False),
                        produces="out"),
                   Step("zopflipng-alpha", "zopflipng",
                        lambda ctx: _zopflipng_args(ctx, lossy_transparent=True),
                        produces="out", optional=True)),
                  ("optipng", "zopflipng")),
            Chain("optipng+advdef-zopfli",
                  (optipng, _advdef_step("advdef-zopfli", "-4")),
                  ("optipng", "advdef")),
            _oxipng_chain(mode),
            # Цепочки Advanced участвуют и здесь. Иначе обещание «Xtreme не хуже
            # Advanced» держится случайно: измерено, что на 16-битном сером
            # изображении libdeflate из advdef -z2 обыгрывает zopfli, и Xtreme
            # выдавал 314 байт против 313. Набор кандидатов медленного режима
            # обязан быть надмножеством быстрого — тогда инвариант структурный.
            *_posix_advanced_chains(suffix="-fast"),
        )
        note = ("optipng %s + zopflipng x%d"
                % (OPTIPNG_XTREME, cfg.xtreme_iterations))
    return Recipe("png", mode, "Advanced" if mode == 1 else "Xtreme", chains,
                  note=note, lossless_class=opts.lossless_class)


def _truepng_args(ctx, level_flags: Sequence[str]) -> list:
    opts = ctx.cfg.png_mode(ctx.mode)
    return ["-y", "-i0", "-zw7", *level_flags, "-f0,5", "-fs:1",
            *opts.legacy_flags, "-force", "-out", str(ctx.out), str(ctx.work)]


def _windows_deflopt() -> Step:
    return Step("deflopt", "deflopt",
                lambda ctx: ["-k", str(ctx.work)], optional=True)


def _windows_strip() -> Step:
    """Удаление чанков как шаг цепочки.

    В 2.7 это был отдельный проход по УЖЕ записанному файлу назначения
    (`iCatalyst.bat:748`): если проход падал, оригинал был уже перезаписан.
    """
    return Step("truepng-strip", "truepng",
                lambda ctx: (["-nz", "-md", "remove", "all", str(ctx.work)]
                             if ctx.cfg.pngtags == cfgmod.STRIP_ALL else None),
                optional=True)


def _windows_advanced_steps() -> tuple:
    deflopt = _windows_deflopt()
    return (
        Step("truepng", "truepng",
             lambda ctx: _truepng_args(ctx, ("-zc7", "-zm8", "-zs0,1,3")),
             produces="out"),
        deflopt,
        Step("advdef", "advdef", lambda ctx: ["-z2", str(ctx.work)], optional=True),
        deflopt,
        _windows_strip(),
    )


def _png_windows(mode: int, cfg) -> Recipe:
    """Windows-цепочка сохраняется дословно: результат остаётся байт в байт."""
    opts = cfg.png_mode(mode)
    deflopt = _windows_deflopt()
    if mode == 1:
        steps = _windows_advanced_steps()
    else:
        steps = (
            Step("truepng", "truepng",
                 lambda ctx: _truepng_args(ctx, ("-zc7", "-zm5-9", "-zs0,1,3")),
                 produces="out", parse=_parse_truepng_log),
            Step("pngwolf", "pngwolf", _pngwolf_args, produces="out", optional=True),
            deflopt,
            _windows_strip(),
        )
    chains = (Chain("truepng", steps, ("truepng",)), _oxipng_chain(mode))
    if mode == 2:
        # Как и на POSIX, медленный режим обязан гонять и быстрые цепочки:
        # иначе «Xtreme не хуже Advanced» — совпадение, а не свойство.
        chains += (Chain("truepng-fast", _windows_advanced_steps(), ("truepng",)),
                   _oxipng_chain(1, suffix="-fast"))
    return Recipe("png", mode, "Advanced" if mode == 1 else "Xtreme", chains,
                  lossless_class=opts.lossless_class)


def _parse_truepng_log(ctx, proc) -> None:
    """Выцепить из лога TruePNG выбранные параметры zlib.

    Строка выглядит как `zc: 9  zm: 8  zs: 1`; в 2.7 её разбирал
    `for /f "tokens=2,4,6..."` (`iCatalyst.bat:724`). Значения передаются
    pngwolf, чтобы тот не искал их заново.
    """
    text = (proc.stdout or b"").decode("utf-8", "replace")
    match = re.search(r"zc:\s*(\d+)\s*zm:\s*(\d+)\s*zs:\s*(\d+)", text)
    if match:
        zc, zm, zs = (int(g) for g in match.groups())
        # Правило из iCatalyst.bat:729 — стратегии выше 1 приводятся к 1, но
        # число итераций поднимается с 10 до 15.
        iterations = 10
        if zs > 1:
            zs, iterations = 1, 15
        ctx.scratch.update(zc=zc, zm=zm, zs=zs, iterations=iterations)


def _pngwolf_args(ctx) -> list:
    tool = ctx.tools.find("pngwolf")
    zc = ctx.scratch.get("zc", 9)
    zm = ctx.scratch.get("zm", 8)
    zs = ctx.scratch.get("zs", 0)
    iterations = ctx.scratch.get("iterations", ctx.cfg.xtreme_iterations)
    common = ["--max-stagnate-time=0", "--max-evaluations=1",
              "--in=%s" % ctx.work, "--out=%s" % ctx.out]
    if tool is not None and tool.has("new-cli"):
        return [
            "--out-deflate=zopfli,iter=%d,maxsplit=0" % iterations,
            "--estimator=zlib,level=%d,memlevel=%d,strategy=%d,window=15" % (zc, zm, zs),
            *common,
        ]
    return [
        "--zopfli-iter=%d" % iterations, "--zopfli-maxsplit=0", "--zlib-window=15",
        "--zlib-level=%d" % zc, "--zlib-memlevel=%d" % zm, "--zlib-strategy=%d" % zs,
        *common,
    ]


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

def _jpeg_copy_flag(cfg) -> list:
    """Что jpegtran должен перенести из исходника.

    В 2.7 это делалось в два приёма: `-copy all`, а затем отдельный
    `jpegstripper -y` по готовому файлу. Один флаг вместо этого — на один
    инструмент и на одну перезапись меньше, и метаданные не удаляются с уже
    записанного результата.
    """
    if cfg.jpegtags == cfgmod.STRIP_ALL:
        return ["-copy", "none"]
    if cfg.jpegtags == cfgmod.STRIP_KEEP_ICC:
        return ["-copy", "icc"]
    return ["-copy", "all"]


def _jpeg_args(ctx, target: str) -> list:
    tool = ctx.tools.find("jpegtran")
    mozjpeg = tool is not None and tool.has("mozjpeg")
    args = []
    if target == "baseline":
        # `-revert` означает «выключить нестандартные дефолты MozJPEG», то есть
        # запросить поведение обычного libjpeg. У libjpeg-turbo такого флага
        # нет и не нужно: его поведение и есть стандартное.
        if mozjpeg:
            args.append("-revert")
        args.append("-optimize")
    else:
        # MozJPEG по умолчанию выдаёт progressive со своим подобранным
        # сценарием сканов — в 2.7 на это и полагались. libjpeg-turbo по
        # умолчанию последователен, ему нужно сказать явно.
        if not mozjpeg:
            args.append("-progressive")
    args += _jpeg_copy_flag(ctx.cfg)
    args += ["-outfile", str(ctx.out), str(ctx.work)]
    return args


def _jpeg_target(ctx) -> str:
    """Во что кодировать: baseline или progressive."""
    if ctx.mode == 1:
        return "baseline"
    if ctx.mode == 2:
        return "progressive"
    # Режим 3 — «настройки оригинала». Раньше отвечал jpginfo.exe, теперь
    # маркер SOF читается напрямую.
    return "progressive" if ctx.scratch.get("orig_progressive") else "baseline"


def _jpeg(mode: int, cfg) -> Recipe:
    label = {1: "Baseline", 2: "Progressive", 3: "Default"}[mode]
    step = Step("jpegtran", "jpegtran",
                lambda ctx: _jpeg_args(ctx, _jpeg_target(ctx)), produces="out")
    return Recipe("jpg", mode, label,
                  (Chain("jpegtran", (step,), ("jpegtran",)),))


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

def _gifsicle_args(ctx, level: str) -> list:
    args = []
    if ctx.cfg.giftags == cfgmod.STRIP_ALL:
        args += ["--no-comments", "--no-extensions", "--no-names"]
        # `--no-extensions` выбрасывает расширение NETSCAPE, то есть бесконечно
        # циклящийся GIF становится одноразовым. Возвращаем счётчик обратно.
        loop = ctx.scratch.get("loop_count")
        if loop is not None:
            args.append("--loopcount=%s" % ("forever" if loop == 0 else loop))
    args += [level, "--output", str(ctx.out), str(ctx.work)]
    return args


def _gif(mode: int, cfg) -> Recipe:
    return Recipe("gif", mode, "Default", (
        Chain("gifsicle-O0",
              (Step("gifsicle", "gifsicle",
                    lambda ctx: _gifsicle_args(ctx, "--optimize=0"), produces="out"),),
              ("gifsicle",)),
        Chain("gifsicle-O3",
              (Step("gifsicle", "gifsicle",
                    lambda ctx: _gifsicle_args(ctx, "--optimize=3"), produces="out"),),
              ("gifsicle",)),
    ))


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------

def use_windows_profile(cfg) -> bool:
    profile = getattr(cfg, "profile", "auto")
    if profile == "windows":
        return True
    if profile == "posix":
        return False
    return os.name == "nt"


def build(fmt: str, mode: int, cfg, windows: Optional[bool] = None) -> Optional[Recipe]:
    """Собрать рецепт для формата и режима под выбранный профиль."""
    if mode == 0:
        return None
    if windows is None:
        windows = use_windows_profile(cfg)
    if fmt == "png":
        if mode not in (1, 2):
            return None
        # На Windows цепочка TruePNG сохраняется как есть; если сам TruePNG
        # недоступен, поиск уйдёт к POSIX-цепочке через runnable_chains.
        return _png_windows(mode, cfg) if windows else _png_posix(mode, cfg)
    if fmt == "jpg":
        return _jpeg(mode, cfg) if mode in (1, 2, 3) else None
    if fmt == "gif":
        return _gif(mode, cfg) if mode == 1 else None
    return None


def runnable_chains(recipe: Recipe, tools) -> List[Chain]:
    """Цепочки, все обязательные инструменты которых нашлись."""
    return [c for c in recipe.chains if tools.available(*c.requires)]


def fallback_recipe(fmt: str, mode: int, cfg) -> Optional[Recipe]:
    """Рецепт другого профиля как запасной вариант.

    Нужен, например, на Windows без TruePNG: формат не должен отваливаться
    целиком. При явно заданном профиле подмены не происходит — пользователь
    выбрал набор инструментов сознательно, и молча его менять нельзя.
    """
    if getattr(cfg, "profile", "auto") != "auto":
        return None
    return build(fmt, mode, cfg, windows=not use_windows_profile(cfg))
