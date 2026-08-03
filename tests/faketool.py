"""Поддельные оптимизаторы для тестов.

Это самое ценное решение во всей проверке: благодаря ним конвейер, отчёт,
отображение путей и обработка ошибок проверяются **на машине, где не
установлено ни одного настоящего оптимизатора**. Реестр инструментов ищет
бинарники через переопределяемый список каталогов, и `ICATALYST_TOOLS_DIR`
подсовывает сюда эти скрипты.

Подделки не притворяются: они действительно выполняют сжатие без потерь —
пересжимают IDAT на девятом уровне zlib, выбрасывают метаданные JPEG, удаляют
блоки комментариев GIF. Поэтому проверки «результат строго меньше входа» и
«пиксели не изменились» осмысленны, а не подогнаны.

Инъекция сбоев — через окружение:

* `ICATALYST_FAKE_MODE` — `normal`, `grow`, `zero`, `equal`, `fail`, `hang`,
  `garbage`;
* `ICATALYST_FAKE_TARGET` — к какому инструменту применить режим (по умолчанию
  ко всем);
* `ICATALYST_FAKE_MOZJPEG` — заставить поддельный jpegtran объявить поддержку
  `-revert`, то есть выдать себя за MozJPEG.
"""

from __future__ import annotations

import os
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

_CHUNK_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}


def _crc_chunk(name: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + name + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF))


# ---------------------------------------------------------------------------
# Инъекция сбоев
# ---------------------------------------------------------------------------

def _mode_for(tool: str) -> str:
    mode = os.environ.get("ICATALYST_FAKE_MODE", "normal")
    target = os.environ.get("ICATALYST_FAKE_TARGET")
    if target and target != tool:
        return "normal"
    return mode


def _apply_mode(tool: str, out_path: Optional[Path], payload: bytes) -> Optional[int]:
    """Вернуть код возврата, если режим перехватил работу, иначе None."""
    mode = _mode_for(tool)
    if mode == "normal":
        return None
    if mode == "fail":
        sys.stderr.write("fake %s: injected failure\n" % tool)
        return 1
    if mode == "hang":
        time.sleep(600)
        return 0
    if out_path is None:
        return 0
    if mode == "zero":
        out_path.write_bytes(b"")
    elif mode == "grow":
        out_path.write_bytes(payload + b"\x00" * (len(payload) + 64))
    elif mode == "equal":
        out_path.write_bytes(payload)
    elif mode == "garbage":
        out_path.write_bytes(b"not an image at all")
    return 0


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _png_chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        name = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        yield name, payload
        pos += 12 + length
        if name == b"IEND":
            return


def _png_optimize(data: bytes, strip: bool) -> bytes:
    """Пересжать IDAT максимальным уровнем zlib, не трогая пиксели."""
    idat = b"".join(p for n, p in _png_chunks(data) if n == b"IDAT")
    raw = zlib.decompress(idat)
    out = [data[:8]]
    for name, payload in _png_chunks(data):
        if name == b"IDAT":
            continue
        if name == b"IEND":
            out.append(_crc_chunk(b"IDAT", zlib.compress(raw, 9)))
            out.append(_crc_chunk(name, payload))
            break
        if strip and name not in _CHUNK_KEEP:
            continue
        out.append(_crc_chunk(name, payload))
    return b"".join(out)


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

_JPEG_STANDALONE = {0xD8, 0x01} | set(range(0xD0, 0xD8))
#: Маркеры, выбрасываемые при `-copy none`: APPn и COM.
_JPEG_METADATA = set(range(0xE0, 0xF0)) | {0xFE}


def _jpeg_strip(data: bytes, copy: str) -> bytes:
    if copy == "all":
        return data
    keep_icc = copy == "icc"
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in _JPEG_STANDALONE or marker == 0xFF:
            pos += 2
            continue
        if marker == 0xD9:
            out += b"\xff\xd9"
            break
        (seg_len,) = struct.unpack_from(">H", data, pos + 2)
        segment = data[pos:pos + 2 + seg_len]
        body = data[pos + 4:pos + 2 + seg_len]
        drop = marker in _JPEG_METADATA
        if keep_icc and marker == 0xE2 and body[:11] == b"ICC_PROFILE":
            drop = False
        if not drop:
            out += segment
        pos += 2 + seg_len
        if marker == 0xDA:
            # Дальше энтропийные данные до маркера конца.
            tail = pos
            while tail < len(data) - 1:
                if data[tail] == 0xFF and data[tail + 1] not in (0x00, 0xFF) and \
                        not (0xD0 <= data[tail + 1] <= 0xD7):
                    break
                tail += 1
            out += data[pos:tail]
            pos = tail
    return bytes(out)


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

def _gif_filter(data: bytes, no_comments: bool, no_extensions: bool,
                loopcount: Optional[str]) -> bytes:
    packed = data[10]
    pos = 13
    out = bytearray(data[:13])
    if packed & 0x80:
        size = 3 * (2 << (packed & 0x07))
        out += data[pos:pos + size]
        pos += size
    if loopcount is not None:
        value = 0 if loopcount == "forever" else int(loopcount)
        out += (b"\x21\xff\x0bNETSCAPE2.0\x03\x01"
                + struct.pack("<H", value) + b"\x00")

    def read_blocks(at: int) -> Tuple[bytes, int]:
        start = at
        while at < len(data):
            size = data[at]
            at += 1
            if size == 0:
                return data[start:at], at
            at += size
        return data[start:], at

    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            out += b"\x3b"
            break
        if block == 0x21:
            label = data[pos + 1]
            payload, after = read_blocks(pos + 2)
            drop = False
            if label == 0xFE and no_comments:
                drop = True
            elif label == 0xFF and no_extensions:
                # Именно это в 2.7 превращало бесконечно циклящийся GIF в
                # одноразовый: NETSCAPE-расширение выбрасывалось.
                drop = True
            elif label not in (0xF9, 0xFE, 0xFF) and no_extensions:
                drop = True
            if not drop:
                out += data[pos:after]
            pos = after
            continue
        if block != 0x2C:
            out += data[pos:]
            break
        header_end = pos + 10
        lpacked = data[pos + 9]
        out += data[pos:header_end]
        pos = header_end
        if lpacked & 0x80:
            size = 3 * (2 << (lpacked & 0x07))
            out += data[pos:pos + size]
            pos += size
        out += bytes([data[pos]])
        payload, pos = read_blocks(pos + 1)
        out += payload
    return bytes(out)


# ---------------------------------------------------------------------------
# Разбор аргументов и сами инструменты
# ---------------------------------------------------------------------------

def _positionals(args: List[str], with_value: Tuple[str, ...] = ()) -> List[str]:
    out = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in with_value:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out


def _opt_value(args: List[str], name: str) -> Optional[str]:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def tool_optipng(args: List[str]) -> int:
    # Ответы на зонды повторяют настоящий optipng 0.7.8 буквально: название
    # программы печатает только `-v`, а вывод `-h` начинается со «Synopsis» и
    # названия не содержит вообще. Подделка, отвечающая иначе, не проверяла бы
    # поиск инструментов, а маскировала бы его ошибки.
    if "-v" in args or "-version" in args:
        print("OptiPNG version 0.7.8 (fake)")
        print("Copyright (C) 2001-2023 Cosmin Truta and the Contributing Authors.")
        return 0
    if "-h" in args or "-help" in args or not args:
        print("Synopsis:")
        print("    optipng [options] files ...")
        print("Options:")
        print("    -o <level>  optimization level (0-7)")
        print("    -quiet, -silent  run in quiet mode")
        print("    -out <file>  write output file to <file>")
        print("    -zc <levels>  zlib compression levels (1-9)")
        print("    -zm <levels>  zlib memory levels (1-9)")
        print("    -zs <strategies>  zlib compression strategies (0-3)")
        print("    -zw <size>  zlib window size")
        print("    -nb  no bit depth reduction")
        print("    -nc  no color type reduction")
        print("    -np  no palette reduction")
        print('    -strip <objects>  strip metadata objects (e.g. "all")')
        return 0
    out = _opt_value(args, "-out")
    rest = _positionals(args, with_value=("-out", "-zc", "-zm", "-zs", "-zw", "-strip", "-o"))
    if out is None or not rest:
        sys.stderr.write("fake optipng: bad arguments: %r\n" % (args,))
        return 2
    src = Path(rest[-1])
    dst = Path(out)
    data = src.read_bytes()
    intercepted = _apply_mode("optipng", dst, data)
    if intercepted is not None:
        return intercepted
    strip = _opt_value(args, "-strip") == "all"
    dst.write_bytes(_png_optimize(data, strip))
    return 0


def tool_advdef(args: List[str]) -> int:
    # Программа представляется названием пакета, а не своим, и Debian собирает
    # её без версии — печатается буквально «vnone». Подделка повторяет это,
    # потому что именно на таком выводе зонд и спотыкался.
    if "--version" in args or "-V" in args:
        print("advancecomp vnone by Andrea Mazzoleni, http://www.advancemame.it")
        return 0
    if "--help" in args or "-h" in args or not args:
        print("advancecomp vnone by Andrea Mazzoleni, http://www.advancemame.it")
        print("Usage: advpng [options] [FILES...]")
        print("Modes:")
        print("  -z, --recompress      Recompress the specified files")
        print("Options:")
        print("  -1, --shrink-fast     Compress fast (zlib)")
        print("  -2, --shrink-normal   Compress normal (libdeflate)")
        print("  -3, --shrink-extra    Compress extra (7z)")
        print("  -4, --shrink-insane   Compress extreme (zopfli)")
        print("  -q, --quiet           Don't print on the console")
        return 0
    files = _positionals(args)
    if not files:
        sys.stderr.write("fake advdef: no files\n")
        return 2
    target = Path(files[-1])
    data = target.read_bytes()
    intercepted = _apply_mode("advdef", target, data)
    if intercepted is not None:
        return intercepted
    target.write_bytes(_png_optimize(data, strip=False))
    return 0


def tool_zopflipng(args: List[str]) -> int:
    if not args:
        print("zopflipng (fake): Usage: zopflipng [options] infile outfile")
        print("  --iterations=N  --filters=...  --lossy_transparent")
        print("  --keepchunks=a,b  -y")
        return 0
    files = _positionals(args)
    if len(files) < 2:
        sys.stderr.write("fake zopflipng: need infile and outfile\n")
        return 2
    src, dst = Path(files[0]), Path(files[1])
    data = src.read_bytes()
    intercepted = _apply_mode("zopflipng", dst, data)
    if intercepted is not None:
        return intercepted
    strip = not any(a.startswith("--keepchunks=") for a in args)
    dst.write_bytes(_png_optimize(data, strip))
    return 0


def tool_gifsicle(args: List[str]) -> int:
    if "--version" in args:
        print("LCDF Gifsicle 1.94 (fake)")
        return 0
    if not args:
        print("gifsicle (fake): Usage: gifsicle [options] file")
        return 0
    out = _opt_value(args, "--output")
    files = _positionals(args, with_value=("--output", "-o"))
    if out is None or not files:
        sys.stderr.write("fake gifsicle: bad arguments: %r\n" % (args,))
        return 2
    src, dst = Path(files[-1]), Path(out)
    data = src.read_bytes()
    intercepted = _apply_mode("gifsicle", dst, data)
    if intercepted is not None:
        return intercepted
    loop = None
    for arg in args:
        if arg.startswith("--loopcount="):
            loop = arg.split("=", 1)[1]
    dst.write_bytes(_gif_filter(
        data,
        no_comments="--no-comments" in args,
        no_extensions="--no-extensions" in args,
        loopcount=loop,
    ))
    return 0


def tool_jpegtran(args: List[str]) -> int:
    if "-h" in args or "-help" in args or not args:
        print("jpegtran (fake): usage: jpegtran [switches] inputfile")
        print("  -optimize      Optimize Huffman table")
        print("  -progressive   Create progressive JPEG file")
        print("  -copy none|comments|icc|all   Copy markers")
        if os.environ.get("ICATALYST_FAKE_MOZJPEG"):
            print("  -revert        Revert to standard defaults")
        return 0
    out = _opt_value(args, "-outfile")
    files = _positionals(args, with_value=("-outfile", "-copy"))
    if out is None or not files:
        sys.stderr.write("fake jpegtran: bad arguments: %r\n" % (args,))
        return 2
    src, dst = Path(files[-1]), Path(out)
    data = src.read_bytes()
    intercepted = _apply_mode("jpegtran", dst, data)
    if intercepted is not None:
        return intercepted
    dst.write_bytes(_jpeg_strip(data, _opt_value(args, "-copy") or "comments"))
    return 0


def tool_oxipng(args: List[str]) -> int:
    """Подделка oxipng. Возможности объявляются в справке, как у настоящего."""
    if "--version" in args:
        print("oxipng 10.1.1 (fake)")
        return 0
    if "--help" in args or not args:
        print("oxipng 10.1.1 (fake)")
        print("Options:")
        print("  -o, --opt <level>   optimization level (0-6, max)")
        print("  -Z, --zopfli        use zopfli backend")
        print("  -a, --alpha         alter colour of fully transparent pixels")
        print("      --strip <mode>  safe, all or a list of chunks")
        print("      --nc --nb --np  keep colour type / bit depth / palette")
        print("  -t, --threads <n>")
        print("      --out <file>")
        return 0
    out = _opt_value(args, "--out")
    rest = _positionals(args, with_value=("--out", "-o", "--opt", "--strip",
                                          "-t", "--threads"))
    if out is None or not rest:
        sys.stderr.write("fake oxipng: bad arguments: %r\n" % (args,))
        return 2
    src, dst = Path(rest[-1]), Path(out)
    data = src.read_bytes()
    intercepted = _apply_mode("oxipng", dst, data)
    if intercepted is not None:
        return intercepted
    strip = _opt_value(args, "--strip") == "all"
    dst.write_bytes(_png_optimize(data, strip))
    return 0


def tool_truepng(args: List[str]) -> int:
    """Подделка закрытого TruePNG.

    Зонд её не запускает (`never_execute`), поэтому справка не нужна. Зато нужны
    две вещи: писать в `-out` и печатать в лог выбранные параметры zlib в том же
    виде, в каком их выцепляет скрейп — `zc: N  zm: N  zs: N`.
    """
    if "-md" in args:
        # Режим удаления чанков: работает по файлу на месте.
        files = _positionals(args, with_value=("-md",))
        files = [f for f in files if f not in ("remove", "all")]
        if not files:
            sys.stderr.write("fake truepng: no file to strip\n")
            return 2
        target = Path(files[-1])
        data = target.read_bytes()
        intercepted = _apply_mode("truepng", target, data)
        if intercepted is not None:
            return intercepted
        target.write_bytes(_png_optimize(data, strip=True))
        return 0

    out = _opt_value(args, "-out")
    rest = _positionals(args, with_value=("-out",))
    if out is None or not rest:
        sys.stderr.write("fake truepng: bad arguments: %r\n" % (args,))
        return 2
    src, dst = Path(rest[-1]), Path(out)
    data = src.read_bytes()
    # Режим Xtreme узнаётся по диапазону -zm5-9; тогда TruePNG печатает лог,
    # который в 2.7 разбирал `for /f "tokens=2,4,6..."`.
    if any(a.startswith("-zm") and "-" in a[3:] for a in args):
        print("Attempting zlib parameters...")
        print("  zc: 9  zm: 8  zs: %s" % os.environ.get("ICATALYST_FAKE_ZS", "3"))
    intercepted = _apply_mode("truepng", dst, data)
    if intercepted is not None:
        return intercepted
    dst.write_bytes(_png_optimize(data, strip=False))
    return 0


def tool_deflopt(args: List[str]) -> int:
    """Подделка закрытого DeflOpt: правит файл на месте, `-k` сохраняет чанки."""
    files = _positionals(args)
    if not files:
        sys.stderr.write("fake deflopt: no files\n")
        return 2
    target = Path(files[-1])
    data = target.read_bytes()
    intercepted = _apply_mode("deflopt", target, data)
    if intercepted is not None:
        return intercepted
    target.write_bytes(_png_optimize(data, strip=False))
    return 0


def tool_pngwolf(args: List[str]) -> int:
    """Подделка pngwolf-zopfli.

    Умеет притворяться и версией 1.0.1 с плоскими флагами, и 1.1.2 с
    подопциями: `ICATALYST_FAKE_PNGWOLF_CLI=legacy|new`. Это единственный
    способ проверить, что зонд определяет форму CLI и argv строится верно.
    """
    new_cli = os.environ.get("ICATALYST_FAKE_PNGWOLF_CLI", "new") != "legacy"
    if "--help" in args or not args:
        print("pngwolf (fake)")
        print("  --in=<file>   input file")
        print("  --out=<file>  output file")
        print("  --max-stagnate-time=<n>  --max-evaluations=<n>")
        if new_cli:
            print("  --out-deflate=<name[,opt..]>  lib for output")
            print("  --estimator=<name[,opt..]>    lib for estimator")
        else:
            print("  --zopfli-iter=<n>  --zopfli-maxsplit=<n>")
            print("  --zlib-level=<n>  --zlib-memlevel=<n>")
            print("  --zlib-strategy=<n>  --zlib-window=<n>")
        return 0
    src = _opt_value(args, "--in")
    dst = _opt_value(args, "--out")
    if not src or not dst:
        sys.stderr.write("fake pngwolf: bad arguments: %r\n" % (args,))
        return 2
    # Проверяем, что нам передали форму флагов, соответствующую нашей версии.
    has_new = any(a.startswith("--out-deflate=") for a in args)
    has_legacy = any(a.startswith("--zopfli-iter=") for a in args)
    if new_cli and not has_new:
        sys.stderr.write("fake pngwolf 1.1.x got legacy flags: %r\n" % (args,))
        return 2
    if not new_cli and not has_legacy:
        sys.stderr.write("fake pngwolf 1.0.x got sub-option flags: %r\n" % (args,))
        return 2
    data = Path(src).read_bytes()
    intercepted = _apply_mode("pngwolf", Path(dst), data)
    if intercepted is not None:
        return intercepted
    Path(dst).write_bytes(_png_optimize(data, strip=False))
    return 0


TOOLS = {
    "optipng": tool_optipng,
    "advdef": tool_advdef,
    "zopflipng": tool_zopflipng,
    "gifsicle": tool_gifsicle,
    "jpegtran": tool_jpegtran,
    "oxipng": tool_oxipng,
    # Закрытые Windows-утилиты: подделки позволяют проверять Windows-цепочку на
    # любой машине, а не только на windows-раннере.
    "truepng": tool_truepng,
    "deflopt": tool_deflopt,
    "pngwolf": tool_pngwolf,
}


def main(name: str, args: List[str]) -> int:
    handler = TOOLS.get(name)
    if handler is None:
        sys.stderr.write("fake tool %r is not implemented\n" % name)
        return 127
    try:
        return handler(list(args))
    except Exception as exc:  # подделка не должна маскировать ошибку теста
        sys.stderr.write("fake %s crashed: %r\n" % (name, exc))
        return 3


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[0]).stem, sys.argv[1:]))
