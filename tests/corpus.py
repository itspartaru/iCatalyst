"""Генератор тестового корпуса: злые имена и настоящие изображения.

Всё строится стандартной библиотекой. PNG собирается на `zlib` и `struct`,
GIF — с честным (пусть и не сжимающим) кодировщиком LZW, JPEG — с собственными
минимальными таблицами Хаффмана, что спецификация прямо разрешает. Благодаря
этому корпус воспроизводится на любой машине и не требует бинарных фикстур в
репозитории.

Имена подобраны по разбору исходного бага. Под кодом 2.7 **каждое** из них
попадало либо в корзину «Images with characters» (белый список
`filter.js:4` отвергал символ), либо, что хуже, молча в «Images are not found»
(символ отсутствует в cp866, `dir` заменял его на `?`, а `?` белым списком
разрешён). Пустота этих корзин и есть регрессионный тест.
"""

from __future__ import annotations

import os
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(name: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + name + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF))


def png_bytes(width: int = 8, height: int = 8, color_type: int = 6,
              bit_depth: int = 8, seed: int = 0,
              palette: Optional[Sequence[Tuple[int, int, int]]] = None,
              trns: Optional[bytes] = None,
              text: Optional[bytes] = None,
              level: int = 0, interlace: int = 0) -> bytes:
    """Собрать корректный PNG.

    `level=0` (без сжатия) — по умолчанию нарочно: тогда любой настоящий или
    поддельный оптимизатор действительно уменьшает файл, и проверки «результат
    строго меньше» становятся осмысленными.
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    max_value = (1 << bit_depth) - 1
    rows = []
    for y in range(height):
        samples = []
        for x in range(width):
            base = (x * 7 + y * 13 + seed) % (max_value + 1)
            if color_type == 3:
                samples.append(base % max(1, len(palette or [(0, 0, 0)])))
            elif color_type == 0:
                samples.append(base)
            elif color_type == 4:
                samples += [base, max_value if (x + y) % 5 else 0]
            elif color_type == 2:
                samples += [base, (base * 3) % (max_value + 1),
                            (base * 5) % (max_value + 1)]
            else:
                samples += [base, (base * 3) % (max_value + 1),
                            (base * 5) % (max_value + 1),
                            max_value if (x + y) % 4 else 0]
        rows.append(_pack_samples(samples, bit_depth))
    raw = b"".join(b"\x00" + row for row in rows)

    out = [PNG_SIGNATURE,
           _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth,
                                       color_type, 0, 0, interlace))]
    if color_type == 3:
        entries = palette or [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        out.append(_chunk(b"PLTE", b"".join(bytes(e) for e in entries)))
    if trns is not None:
        out.append(_chunk(b"tRNS", trns))
    out.append(_chunk(b"gAMA", struct.pack(">I", 45455)))
    if text:
        out.append(_chunk(b"tEXt", b"Comment\x00" + text))
    out.append(_chunk(b"IDAT", zlib.compress(raw, level)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def _pack_samples(samples: Sequence[int], bit_depth: int) -> bytes:
    if bit_depth == 8:
        return bytes(samples)
    if bit_depth == 16:
        return b"".join(struct.pack(">H", s) for s in samples)
    per_byte = 8 // bit_depth
    out = bytearray()
    for i in range(0, len(samples), per_byte):
        byte = 0
        for j in range(per_byte):
            value = samples[i + j] if i + j < len(samples) else 0
            byte |= value << (8 - bit_depth * (j + 1))
        out.append(byte)
    return bytes(out)


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

def _lzw_encode(indices: bytes, min_code_size: int) -> bytes:
    """Записать поток LZW без собственно сжатия.

    Каждый пиксель отдаётся своим литеральным кодом. Словарь при этом всё равно
    растёт на стороне декодера, поэтому кодировщик обязан повторять его логику:
    увеличивать разрядность кода в тех же точках и вовремя вставлять CLEAR.
    Так получается заведомо корректный поток без реализации всего алгоритма.
    """
    clear = 1 << min_code_size
    stop = clear + 1
    code_size = min_code_size + 1
    next_code = stop + 1
    bits = 0
    acc = 0
    out = bytearray()

    def emit(code: int) -> None:
        nonlocal bits, acc
        acc |= code << bits
        bits += code_size
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8

    emit(clear)
    for i, index in enumerate(indices):
        emit(index)
        if i:  # декодер добавляет запись начиная со второго кода
            next_code += 1
            if next_code >= (1 << code_size):
                if code_size < 12:
                    code_size += 1
                else:
                    emit(clear)
                    code_size = min_code_size + 1
                    next_code = stop + 1
    emit(stop)
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _blocks(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 255):
        piece = data[i:i + 255]
        out.append(len(piece))
        out += piece
    out.append(0)
    return bytes(out)


def gif_bytes(width: int = 8, height: int = 8, frames: int = 1,
              loop: Optional[int] = 0, comment: Optional[bytes] = b"iCatalyst test",
              delay: int = 10) -> bytes:
    """Собрать корректный GIF, при желании анимированный и с комментарием.

    Комментарий нужен для тестов: поддельный gifsicle уменьшает файл именно за
    счёт его удаления, а значит проверяет и путь удаления метаданных, и
    восстановление счётчика циклов.
    """
    palette = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", width, height, 0x80 | 0x01, 0, 0)  # 4 цвета
    for entry in palette:
        out += bytes(entry)
    if loop is not None:
        out += b"\x21\xff\x0bNETSCAPE2.0" + b"\x03\x01" + struct.pack("<H", loop) + b"\x00"
    if comment:
        out += b"\x21\xfe" + _blocks(comment)
    for frame in range(frames):
        out += b"\x21\xf9\x04" + bytes([0x04, delay & 0xFF, delay >> 8, 0]) + b"\x00"
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0)
        indices = bytes(((x + y + frame) % len(palette))
                        for y in range(height) for x in range(width))
        out += bytes([2]) + _blocks(_lzw_encode(indices, 2))
    out += b"\x3b"
    return bytes(out)


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

def jpeg_bytes(progressive: bool = False, comment: Optional[bytes] = None,
               width: int = 8, height: int = 8) -> bytes:
    """Собрать минимальный корректный JPEG 8x8 в градациях серого.

    Таблицы Хаффмана заданы свои, минимальные: спецификация это разрешает, а
    стандартные таблицы Annex K заняли бы сотню байт констант. Один блок,
    нулевой DC-разностью и сразу EOB.
    """
    def seg(marker: int, payload: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload

    out = bytearray(b"\xff\xd8")
    out += seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    if comment:
        out += seg(0xFE, comment)
    # DQT: точность 0, таблица 0, все коэффициенты 16.
    out += seg(0xDB, b"\x00" + bytes([16] * 64))
    sof = 0xC2 if progressive else 0xC0
    out += seg(sof, struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0]))
    # DHT DC таблица 0: два кода длины 2 для символов 0 и 1.
    bits = bytes([0, 2] + [0] * 14)
    out += seg(0xC4, b"\x00" + bits + bytes([0x00, 0x01]))
    # DHT AC таблица 0: два кода длины 2 для EOB и (run 0, size 1).
    out += seg(0xC4, b"\x10" + bits + bytes([0x00, 0x01]))
    if progressive:
        # Первый скан прогрессивного изображения: только DC, Al=0.
        out += seg(0xDA, bytes([1, 1, 0x00, 0, 0, 0x00]))
        out += b"\x0f"
    else:
        out += seg(0xDA, bytes([1, 1, 0x00, 0, 63, 0x00]))
        # DC символ 0 (код '00') + EOB (код '00'), добито единицами до байта.
        out += b"\x0f"
    out += b"\xff\xd9"
    return bytes(out)


# ---------------------------------------------------------------------------
# Злые имена
# ---------------------------------------------------------------------------

#: (имя, платформы, где его нельзя создать)
NASTY_DIRS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Простые файлы", ()),
    # Проводник вставляет длинное тире сам: «Фото — копия». В cp866 его нет,
    # `dir` подменял его на `-`, и весь каталог исчезал молча.
    ("Тест — тире", ()),
    ("«Кавычки»", ()),
    ("Ім’я з апострофом", ()),
    # `і` и `ґ` отсутствуют в cp866 — молчаливая потеря; `є` и `ї` в cp866 есть,
    # но не были в белом списке — шумный отказ.
    ("Ґуля і Їжак", ()),
    ("Єдиний Ї", ()),
    # 0x98 — это `Ш` в cp866 и единственная неопределённая позиция в cp1251.
    ("Школа", ()),
    ("dots…ellipsis", ()),
    # Неразрывный пробел: в списке отказов 2.7 он визуально неотличим
    # от обычного, поэтому пользователь не мог понять причину отказа.
    ("NBSP name", ()),
    # Скобки Проводник создаёт сам: «— копия (2)». Белый список их отвергал.
    ("(parens)", ()),
    ("[brackets]", ()),
    ("{braces}", ()),
    ("!bang!", ()),
    ("and&sign", ()),
    ("100%percent", ()),
    ("semi;colon", ()),
    ("caret^hat", ()),
    ("tick'quote", ()),
    ("folder with spaces", ()),
    ("emoji🎨🔥", ()),
    ("CJK日本語漢字", ()),
    ("combining_é", ()),
    (".dotdir", ()),
    ("A" * 250, ()),
]

#: (имя файла, формат, платформы-исключения)
NASTY_FILES: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("лого.png", "png", ()),
    ("Ёлка.PNG", "png", ()),
    ("Ім’я—файл….jpg", "jpg", ()),
    ("photo (1).jpg", "jpg", ()),
    ("photo & co.jpeg", "jpg", ()),
    ("100%.jpe", "jpg", ()),
    ("анимация.gif", "gif", ()),
    ("image.GIF", "gif", ()),
    ("spaces   inside.png", "png", ()),
    (".hidden.png", "png", ()),
    ("emoji🎉.png", "png", ()),
    ("Шишкин.png", "png", ()),
    ("tab\tinside.png", "png", ("nt",)),
    ("trailing_space .png", "png", ("nt",)),
]


def _allowed(skip_on: Sequence[str]) -> bool:
    return os.name not in skip_on and sys.platform not in skip_on


def build_corpus(root: Path, deep_path: bool = True) -> Dict[str, List[Path]]:
    """Создать корпус. Вернуть отображение формат → созданные файлы.

    Имена, которые файловая система отвергает физически, тихо пропускаются:
    цель — проверить нашу обработку, а не возможности ФС.
    """
    root = Path(root)
    created: Dict[str, List[Path]] = {"png": [], "jpg": [], "gif": [], "other": []}
    directories: List[Path] = [root]
    for name, skip_on in NASTY_DIRS:
        if not _allowed(skip_on):
            continue
        path = root / name
        try:
            path.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError):
            continue
        directories.append(path)

    if deep_path:
        # Полный путь длиннее 300 символов: в 2.7 `dir /s` спокойно выдавал
        # такое, а `if not exist` и ANSI-утилиты обрывались на 260.
        deep = root
        for i in range(6):
            deep = deep / ("уровень_%d_%s" % (i, "д" * 40))
        try:
            deep.mkdir(parents=True, exist_ok=True)
            directories.append(deep)
        except (OSError, ValueError):
            pass

    index = 0
    for slot, directory in enumerate(directories):
        for offset, (name, fmt, skip_on) in enumerate(NASTY_FILES):
            if not _allowed(skip_on):
                continue
            index += 1
            # В корень кладём все имена, в остальные каталоги — по несколько,
            # со сдвигом, чтобы каждое имя встретилось в каком-нибудь каталоге.
            if slot and (offset + slot) % 4:
                continue
            target = directory / name
            try:
                target.write_bytes(_payload(fmt, index))
            except (OSError, ValueError):
                continue
            created[fmt].append(target)

    # Отдельные особые случаи.
    extras = [
        ("broken.png", b"", "other"),
        ("truncated.png", png_bytes()[:40], "other"),
        ("actually_a_png.jpg", png_bytes(seed=3), "other"),
        ("Logo.png", png_bytes(seed=5), "png"),
    ]
    for name, data, bucket in extras:
        target = root / name
        try:
            target.write_bytes(data)
        except (OSError, ValueError):
            continue
        created[bucket].append(target)
    # Имя, отличающееся только регистром: на Linux это два разных файла.
    if os.name != "nt" and sys.platform != "darwin":
        target = root / "logo.png"
        target.write_bytes(png_bytes(seed=6))
        created["png"].append(target)
    return created


def _payload(fmt: str, index: int) -> bytes:
    if fmt == "png":
        variants = (
            dict(color_type=6, bit_depth=8),
            dict(color_type=2, bit_depth=8),
            dict(color_type=3, bit_depth=4),
            dict(color_type=0, bit_depth=1),
            dict(color_type=4, bit_depth=8),
            dict(color_type=0, bit_depth=16),
        )
        kwargs = dict(variants[index % len(variants)])
        # 32x32, а не 8x8: на крошечных изображениях «без сжатия» и «уровень 9»
        # дают одинаковую длину, и проверки «результат строго меньше» становятся
        # бессодержательными.
        return png_bytes(width=32, height=32, seed=index, text=b"x" * 200, **kwargs)
    if fmt == "gif":
        return gif_bytes(width=24, height=16, frames=1 + index % 3,
                         loop=0 if index % 2 else None)
    return jpeg_bytes(progressive=bool(index % 2), comment=b"y" * 300)
