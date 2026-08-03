"""Чтение `Tools/config.ini` с сохранением совместимости.

Файл остаётся ровно там же и с теми же ключами, что у версии 2.7, — существующим
пользователям править ничего не нужно. При этом legacy-строки `advanced=/a0 /g0`
и `xtreme=/a1 /g0` специфичны для TruePNG и на не-Windows не значат ничего,
поэтому из них выводятся платформенно-нейтральные семантические параметры.

Файл никогда не перезаписывается: он в cp1251 с CRLF, и `.gitattributes`
намеренно хранит его байты как есть.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import app_directory

#: Значения, которые прежний `:readini` и `if /i` считали истиной.
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0", ""})

#: Общесистемная настройка из пакета. Вынесена в константу, чтобы тесты могли
#: её подменить: обращаться в настоящий /etc из тестов нельзя.
SYSTEM_CONFIG = Path("/etc/icatalyst/config.ini")

#: Режимы удаления метаданных. `true`/`false` — то, что писали в 2.7.
STRIP_ALL = "all"
STRIP_KEEP_ICC = "keep-icc"
STRIP_NONE = "none"


class ConfigError(Exception):
    """Некорректное значение в файле конфигурации."""


def _to_bool(raw: str, key: str, default: bool) -> bool:
    value = (raw or "").strip().lower()
    if value == "":
        # Пустое значение — то же, что отсутствие ключа: в 2.7 `if /i "%x%" equ
        # "true"` на пустой переменной просто не срабатывало.
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError("ключ %s: ожидалось true или false, получено %r" % (key, raw))


def _to_strip(raw: str, key: str) -> str:
    value = (raw or "").strip().lower()
    if value in _TRUE or value == STRIP_ALL:
        return STRIP_ALL
    if value == STRIP_KEEP_ICC:
        return STRIP_KEEP_ICC
    if value in _FALSE or value == STRIP_NONE:
        return STRIP_NONE
    raise ConfigError(
        "ключ %s: ожидалось true, false, all, keep-icc или none, получено %r" % (key, raw)
    )


@dataclass
class PngModeOptions:
    """Параметры одного режима PNG, выведенные из legacy-строки TruePNG."""

    #: `/a1` — переписывать RGB под полностью прозрачными пикселями. Результат
    #: визуально идентичен, но не побитово; в поставляемом config.ini это
    #: включено для Xtreme.
    dirty_transparency: bool = False
    #: `/g0` remove, `/g1` apply, `/g2` keep.
    gamma: str = "remove"
    #: `/nc`, `/nb`, `/np` — запрет менять тип цвета, глубину, палитру.
    keep_colortype: bool = False
    keep_bitdepth: bool = False
    keep_palette: bool = False
    #: Исходная строка, передаваемая TruePNG на Windows дословно.
    legacy_flags: tuple = ()

    @property
    def lossless_class(self) -> str:
        return "visible-exact" if self.dirty_transparency else "bit-exact"


def parse_truepng_flags(raw: str) -> PngModeOptions:
    """Разобрать строку вида `/a1 /g0 /nc` в семантические параметры."""
    opts = PngModeOptions(legacy_flags=tuple((raw or "").split()))
    for flag in opts.legacy_flags:
        low = flag.lower()
        if low in ("/a0", "/a1"):
            opts.dirty_transparency = low == "/a1"
        elif low in ("/g0", "/g1", "/g2"):
            opts.gamma = {"/g0": "remove", "/g1": "apply", "/g2": "keep"}[low]
        elif low == "/na":
            # «не менять RGB полностью прозрачных пикселей» — противоположность /a1
            opts.dirty_transparency = False
        elif low == "/nc":
            opts.keep_colortype = True
        elif low == "/nb":
            opts.keep_bitdepth = True
        elif low == "/np":
            opts.keep_palette = True
    return opts


def render_truepng_flags(opts: PngModeOptions) -> tuple:
    """Собрать флаги TruePNG обратно из семантических параметров.

    Нужно, когда пользователь задал семантический ключ: на Windows он должен
    доехать до TruePNG, а не быть проигнорирован.
    """
    flags = ["/a1" if opts.dirty_transparency else "/a0"]
    flags.append({"remove": "/g0", "apply": "/g1", "keep": "/g2"}[opts.gamma])
    if opts.keep_colortype:
        flags.append("/nc")
    if opts.keep_bitdepth:
        flags.append("/nb")
    if opts.keep_palette:
        flags.append("/np")
    return tuple(flags)


@dataclass
class Config:
    #: 0 означает «по числу процессоров», как `%NUMBER_OF_PROCESSORS%` в 2.7.
    thread: int = 0
    #: Сырое значение: `true` (спросить), `false` (перезаписать) или путь.
    outdir: str = "true"
    update: bool = False
    #: Таймаут на один вызов инструмента, секунды. В 2.7 зависший инструмент
    #: вешал весь прогон.
    timeout: float = 600.0
    #: `auto`, `tk`, `zenity`, `kdialog`, `terminal`, `none`.
    picker: str = "auto"
    #: Сохранять время изменения исходника. В 2.7 `copy /b` ставил новое.
    preserve_mtime: bool = True
    #: `current` или `legacy` — чем сжимать на Windows.
    toolset: str = "current"
    #: `auto`, `windows` или `posix`. Позволяет и принудительно выбрать набор
    #: инструментов, и проверять Windows-цепочку на любой машине.
    profile: str = "auto"

    png_advanced: PngModeOptions = field(default_factory=PngModeOptions)
    png_xtreme: PngModeOptions = field(default_factory=lambda: PngModeOptions(dirty_transparency=True))
    #: Число итераций zopfli в режиме Xtreme — основной регулятор на Linux.
    xtreme_iterations: int = 15

    pngtags: str = STRIP_ALL
    jpegtags: str = STRIP_ALL
    giftags: str = STRIP_ALL

    #: Явные пути к инструментам из секции `[tools]`.
    tool_paths: dict = field(default_factory=dict)
    #: Откуда прочитан файл (None — использованы значения по умолчанию).
    source: Optional[Path] = None

    def png_mode(self, mode: int) -> PngModeOptions:
        return self.png_advanced if mode == 1 else self.png_xtreme


def _section(cp: configparser.ConfigParser, name: str) -> dict:
    """Достать секцию, не завися от регистра её имени.

    configparser регистрозависим к именам секций, а в реальных config.ini
    встречается и `[PNG]`, и `[png]`.
    """
    for candidate in cp.sections():
        if candidate.lower() == name.lower():
            return dict(cp.items(candidate))
    return {}


def default_config_path(app_dir: Path) -> Path:
    return app_dir / "Tools" / "config.ini"


def find_config(explicit: Optional[str], app_dir: Path) -> Optional[Path]:
    """Порядок поиска: --config, переменная окружения, каталог программы, XDG."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError("файл конфигурации не найден: %s" % path)
        return path
    env = os.environ.get("ICATALYST_CONFIG")
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path
    local = default_config_path(app_dir)
    if local.is_file():
        return local
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            path = Path(appdata) / "iCatalyst" / "config.ini"
            if path.is_file():
                return path
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / "icatalyst" / "config.ini"
        if path.is_file():
            return path
        # Системная настройка из пакета. Идёт после пользовательской намеренно:
        # настройка пользователя должна перекрывать общесистемную. При установке
        # из .deb модуль лежит в /usr/lib/python3/dist-packages, и каталога
        # Tools рядом с ним нет, поэтому без этой ветки пакет читал бы только
        # значения по умолчанию.
        if SYSTEM_CONFIG.is_file():
            return SYSTEM_CONFIG
    return None


def load(explicit: Optional[str] = None, app_dir: Optional[Path] = None) -> Config:
    """Прочитать конфигурацию. Отсутствие файла — не ошибка."""
    app_dir = app_dir or app_directory()
    path = find_config(explicit, app_dir)
    cfg = Config(source=path)
    if path is None:
        return cfg

    cp = configparser.ConfigParser(
        comment_prefixes=(";", "#"),
        inline_comment_prefixes=None,
        # Обязательно: путь в outdir, содержащий `%`, иначе взорвёт разбор.
        interpolation=None,
        strict=False,
    )
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    cp.read_string(text, source=str(path))

    options = _section(cp, "options")
    if "thread" in options:
        try:
            cfg.thread = max(0, int(options["thread"].strip() or 0))
        except ValueError:
            raise ConfigError("ключ thread: ожидалось целое число, получено %r"
                              % options["thread"])
    if "outdir" in options:
        cfg.outdir = options["outdir"].strip()
    if "update" in options:
        cfg.update = _to_bool(options["update"], "update", False)
    if "timeout" in options:
        try:
            cfg.timeout = float(options["timeout"])
        except ValueError:
            raise ConfigError("ключ timeout: ожидалось число секунд")
    if "picker" in options:
        cfg.picker = options["picker"].strip().lower()
    if "preserve_mtime" in options:
        cfg.preserve_mtime = _to_bool(options["preserve_mtime"], "preserve_mtime", True)
    if "profile" in options:
        value = options["profile"].strip().lower()
        if value not in ("auto", "windows", "posix"):
            raise ConfigError("ключ profile: ожидалось auto, windows или posix")
        cfg.profile = value
    if "toolset" in options:
        value = options["toolset"].strip().lower()
        if value not in ("current", "legacy"):
            raise ConfigError("ключ toolset: ожидалось current или legacy")
        cfg.toolset = value

    png = _section(cp, "PNG")
    cfg.png_advanced = parse_truepng_flags(png.get("advanced", "/a0 /g0"))
    cfg.png_xtreme = parse_truepng_flags(png.get("xtreme", "/a1 /g0"))
    # Семантические ключи главнее legacy-строк и на Windows рендерятся обратно.
    for name, opts in (("advanced", cfg.png_advanced), ("xtreme", cfg.png_xtreme)):
        key = "%s_dirty_transparency" % name
        if key in png:
            opts.dirty_transparency = _to_bool(png[key], key, False)
    for opts in (cfg.png_advanced, cfg.png_xtreme):
        if "gamma" in png:
            value = png["gamma"].strip().lower()
            if value not in ("remove", "apply", "keep"):
                raise ConfigError("ключ gamma: ожидалось remove, apply или keep")
            opts.gamma = value
        for key, attr in (("keep_colortype", "keep_colortype"),
                          ("keep_bitdepth", "keep_bitdepth"),
                          ("keep_palette", "keep_palette")):
            if key in png:
                setattr(opts, attr, _to_bool(png[key], key, False))
        opts.legacy_flags = render_truepng_flags(opts)
    if "xtreme_iterations" in png:
        try:
            cfg.xtreme_iterations = max(1, int(png["xtreme_iterations"]))
        except ValueError:
            raise ConfigError("ключ xtreme_iterations: ожидалось целое число")
    if "pngtags" in png:
        cfg.pngtags = _to_strip(png["pngtags"], "pngtags")

    jpeg = _section(cp, "JPEG")
    if "jpegtags" in jpeg:
        cfg.jpegtags = _to_strip(jpeg["jpegtags"], "jpegtags")

    gif = _section(cp, "GIF")
    if "giftags" in gif:
        cfg.giftags = _to_strip(gif["giftags"], "giftags")

    cfg.tool_paths = {k.lower(): v.strip() for k, v in _section(cp, "tools").items()}
    return cfg
