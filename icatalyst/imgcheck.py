"""Разбор PNG, JPEG и GIF средствами стандартной библиотеки.

Модуль решает три задачи сразу, поэтому и вынесен отдельно:

1. **Заменяет `jpginfo.exe`.** Режим `/jpg:3` («настройки оригинала») требует
   знать, baseline изображение или progressive. Закрытая утилита печатала это
   пятым токеном строки; здесь достаточно найти маркер SOF0 (0xC0) или
   SOF2 (0xC2), пройдя цепочку сегментов, а не доверяя `file`.
2. **Структурная проверка результата.** После любой цепочки инструментов
   выясняем, что файл вообще декодируется, размеры не поменялись, а критические
   чанки на месте.
3. **Сравнение пикселей.** Для GIF выполняется всегда (файлы маленькие, а
   `--optimize=3` играет с межкадровой прозрачностью, где ошибка была бы
   видна), для PNG — по флагу `--verify`.

Никаких сторонних зависимостей: `zlib` умеет inflate, а фильтры PNG и LZW GIF
разворачиваются вручную.
"""

from __future__ import annotations

import struct
import zlib
from array import array
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GIF_SIGNATURES = (b"GIF87a", b"GIF89a")

#: Критические чанки PNG. Их потеря — повреждение, а не оптимизация.
PNG_CRITICAL = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})

#: Чанки, влияющие на отображение. Их потеря допустима только если удаление
#: метаданных было запрошено явно.
PNG_RENDERING = frozenset({b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"bKGD", b"sBIT"})

#: Число каналов по типу цвета PNG.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

#: Маркеры JPEG без сегмента данных.
_JPEG_STANDALONE = frozenset({0xD8, 0x01} | set(range(0xD0, 0xD8)))

#: Маркеры «начало кадра». SOF4/SOF8/SOF12 (0xC4, 0xC8, 0xCC) — это DHT, JPG и
#: DAC, а не кадр, поэтому исключены.
_JPEG_SOF = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})

_JPEG_SOF_NAMES = {
    0xC0: "baseline",
    0xC1: "extended-sequential",
    0xC2: "progressive",
    0xC3: "lossless",
    0xC5: "differential-sequential",
    0xC6: "differential-progressive",
    0xC7: "differential-lossless",
    0xC9: "arithmetic-extended-sequential",
    0xCA: "arithmetic-progressive",
    0xCB: "arithmetic-lossless",
    0xCD: "arithmetic-differential-sequential",
    0xCE: "arithmetic-differential-progressive",
    0xCF: "arithmetic-differential-lossless",
}


class ImageError(Exception):
    """Файл не является корректным изображением поддерживаемого формата."""


class Unsupported(ImageError):
    """Формат корректен, но эта его разновидность здесь не разбирается."""


# ---------------------------------------------------------------------------
# Определение формата
# ---------------------------------------------------------------------------

def sniff(data: bytes) -> Optional[str]:
    """Определить формат по содержимому, а не по расширению.

    Нужно потому, что расширение врёт: в корпусе тестов есть `actually_a_png.jpg`,
    а `iCatalyst.bat` раскладывал файлы по цепочкам исключительно по расширению
    и отдавал PNG в jpegtran.
    """
    if data.startswith(PNG_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in GIF_SIGNATURES:
        return "gif"
    return None


def sniff_file(path) -> Optional[str]:
    with open(path, "rb") as fh:
        return sniff(fh.read(8))


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

@dataclass
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    chunks: list = field(default_factory=list)
    idat_size: int = 0

    @property
    def channels(self) -> int:
        return _PNG_CHANNELS[self.color_type]

    @property
    def has_alpha(self) -> bool:
        return self.color_type in (4, 6) or b"tRNS" in self.chunks

    def chunk_names(self) -> frozenset:
        return frozenset(self.chunks)


def iter_png_chunks(data: bytes, verify_crc: bool = True) -> Iterator[tuple]:
    """Пройти чанки PNG, выдавая (имя, полезная нагрузка, смещение).

    CRC проверяется по умолчанию: это самый дешёвый способ поймать порчу,
    а `zlib.crc32` для этого и существует.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ImageError("не PNG: неверная подпись")
    pos = len(PNG_SIGNATURE)
    end = len(data)
    seen_iend = False
    while pos < end:
        if pos + 8 > end:
            raise ImageError("PNG обрывается в заголовке чанка")
        (length,) = struct.unpack_from(">I", data, pos)
        name = data[pos + 4:pos + 8]
        body_at = pos + 8
        if body_at + length + 4 > end:
            raise ImageError("PNG обрывается внутри чанка %r" % name.decode("latin-1"))
        payload = data[body_at:body_at + length]
        if verify_crc:
            (want,) = struct.unpack_from(">I", data, body_at + length)
            got = zlib.crc32(name + payload) & 0xFFFFFFFF
            if got != want:
                raise ImageError(
                    "PNG: неверная CRC чанка %s" % name.decode("latin-1")
                )
        yield name, payload, pos
        pos = body_at + length + 4
        if name == b"IEND":
            seen_iend = True
            break
    if not seen_iend:
        raise ImageError("PNG без чанка IEND")


def read_png(data: bytes, verify_crc: bool = True) -> PngInfo:
    info = None
    chunks = []
    idat_size = 0
    for name, payload, _ in iter_png_chunks(data, verify_crc=verify_crc):
        if info is None:
            if name != b"IHDR":
                raise ImageError("PNG: первым чанком должен быть IHDR")
            if len(payload) != 13:
                raise ImageError("PNG: IHDR неверной длины")
            w, h, depth, ctype, _comp, _filt, inter = struct.unpack(">IIBBBBB", payload)
            if w == 0 or h == 0:
                raise ImageError("PNG: нулевой размер")
            if ctype not in _PNG_CHANNELS:
                raise ImageError("PNG: неизвестный тип цвета %d" % ctype)
            info = PngInfo(w, h, depth, ctype, inter)
        if name == b"IDAT":
            idat_size += len(payload)
        chunks.append(name)
    if info is None:
        raise ImageError("PNG без IHDR")
    if b"IDAT" not in chunks:
        raise ImageError("PNG без данных изображения")
    info.chunks = chunks
    info.idat_size = idat_size
    return info


def _png_idat(data: bytes) -> bytes:
    parts = [p for name, p, _ in iter_png_chunks(data, verify_crc=False) if name == b"IDAT"]
    return zlib.decompress(b"".join(parts))


def _unfilter(raw: bytes, width: int, height: int, bpp_bits: int) -> bytearray:
    """Развернуть построчные фильтры PNG (типы 0..4).

    `bpp_bits` — бит на пиксель; шаг фильтра равен байтам на пиксель, но не
    меньше одного (так требует спецификация для глубин < 8).
    """
    stride = (width * bpp_bits + 7) // 8
    step = max(1, bpp_bits // 8)
    out = bytearray(stride * height)
    pos = 0
    prev_at = -stride
    for y in range(height):
        if pos >= len(raw):
            raise ImageError("PNG: данных меньше, чем строк")
        ftype = raw[pos]
        pos += 1
        line_at = y * stride
        chunk = raw[pos:pos + stride]
        if len(chunk) < stride:
            raise ImageError("PNG: строка %d обрывается" % y)
        pos += stride
        out[line_at:line_at + stride] = chunk
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(step, stride):
                out[line_at + i] = (out[line_at + i] + out[line_at + i - step]) & 0xFF
        elif ftype == 2:
            if y:
                for i in range(stride):
                    out[line_at + i] = (out[line_at + i] + out[prev_at + i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = out[line_at + i - step] if i >= step else 0
                up = out[prev_at + i] if y else 0
                out[line_at + i] = (out[line_at + i] + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = out[line_at + i - step] if i >= step else 0
                b = out[prev_at + i] if y else 0
                c = out[prev_at + i - step] if (y and i >= step) else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out[line_at + i] = (out[line_at + i] + pred) & 0xFF
        else:
            raise ImageError("PNG: неизвестный тип фильтра %d в строке %d" % (ftype, y))
        prev_at = line_at
    return out


def _samples(line: bytes, count: int, depth: int) -> list:
    """Вынуть `count` сэмплов из строки при заданной битовой глубине."""
    if depth == 8:
        return list(line[:count])
    if depth == 16:
        return [(line[2 * i] << 8) | line[2 * i + 1] for i in range(count)]
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    out = []
    for i in range(count):
        byte = line[i // per_byte]
        shift = 8 - depth * (i % per_byte + 1)
        out.append((byte >> shift) & mask)
    return out


#: Смещения и шаги семи проходов Adam7.
_ADAM7 = (
    (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
    (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
)


def png_pixels(data: bytes) -> tuple:
    """Декодировать PNG в (ширина, высота, RGBA как array('H')).

    Все каналы приводятся к 16 битам: 8-битный сэмпл умножается на 257. Это не
    теряет информацию и делает сравнение корректным даже когда оптимизатор
    честно понизил глубину 16→8 (что возможно только если старший и младший
    байты совпадали, а тогда обратное умножение на 257 точно восстанавливает
    исходное значение).
    """
    info = read_png(data, verify_crc=False)
    raw = _png_idat(data)
    palette = None
    trns = None
    for name, payload, _ in iter_png_chunks(data, verify_crc=False):
        if name == b"PLTE":
            palette = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
        elif name == b"tRNS":
            trns = payload
    if info.color_type == 3 and palette is None:
        raise ImageError("PNG: палитровое изображение без PLTE")

    depth = info.bit_depth
    channels = info.channels
    out = array("H", bytes(8 * info.width * info.height))

    def emit(px_at: int, vals: Sequence[int]) -> None:
        ct = info.color_type
        if ct == 3:
            idx = vals[0]
            if idx >= len(palette):
                raise ImageError("PNG: индекс палитры вне диапазона")
            r, g, b = palette[idx]
            a = 255
            if trns is not None and idx < len(trns):
                a = trns[idx]
            out[px_at] = r * 257
            out[px_at + 1] = g * 257
            out[px_at + 2] = b * 257
            out[px_at + 3] = a * 257
            return
        scale = 1 if depth == 16 else 257
        full = 0xFFFF
        if ct == 0:
            g = vals[0] * scale
            a = full
            if trns is not None and len(trns) >= 2:
                (key,) = struct.unpack(">H", trns[:2])
                if vals[0] == key:
                    a = 0
            out[px_at] = out[px_at + 1] = out[px_at + 2] = g
            out[px_at + 3] = a
        elif ct == 4:
            g = vals[0] * scale
            out[px_at] = out[px_at + 1] = out[px_at + 2] = g
            out[px_at + 3] = vals[1] * scale
        elif ct == 2:
            a = full
            if trns is not None and len(trns) >= 6:
                key = struct.unpack(">HHH", trns[:6])
                if tuple(vals[:3]) == key:
                    a = 0
            out[px_at] = vals[0] * scale
            out[px_at + 1] = vals[1] * scale
            out[px_at + 2] = vals[2] * scale
            out[px_at + 3] = a
        else:  # 6 — RGBA
            out[px_at] = vals[0] * scale
            out[px_at + 1] = vals[1] * scale
            out[px_at + 2] = vals[2] * scale
            out[px_at + 3] = vals[3] * scale

    bpp_bits = channels * depth

    if info.interlace == 0:
        planes = _unfilter(raw, info.width, info.height, bpp_bits)
        stride = (info.width * bpp_bits + 7) // 8
        for y in range(info.height):
            line = planes[y * stride:(y + 1) * stride]
            vals = _samples(line, info.width * channels, depth)
            row_at = y * info.width * 4
            for x in range(info.width):
                emit(row_at + x * 4, vals[x * channels:(x + 1) * channels])
        return info.width, info.height, out

    if info.interlace != 1:
        raise Unsupported("PNG: неизвестный метод чередования %d" % info.interlace)

    # Adam7. Нужен потому, что TruePNG вызывается с `-i0` и выдаёт
    # непрозрачённый результат, а исходник вполне может быть чередованным —
    # без деинтерлейсинга сравнить пиксели было бы нечем.
    pos = 0
    for x0, y0, dx, dy in _ADAM7:
        pw = (info.width - x0 + dx - 1) // dx
        ph = (info.height - y0 + dy - 1) // dy
        if pw <= 0 or ph <= 0:
            continue
        stride = (pw * bpp_bits + 7) // 8
        need = (stride + 1) * ph
        planes = _unfilter(raw[pos:pos + need], pw, ph, bpp_bits)
        pos += need
        for sy in range(ph):
            line = planes[sy * stride:(sy + 1) * stride]
            vals = _samples(line, pw * channels, depth)
            y = y0 + sy * dy
            for sx in range(pw):
                x = x0 + sx * dx
                emit((y * info.width + x) * 4, vals[sx * channels:(sx + 1) * channels])
    return info.width, info.height, out


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

@dataclass
class JpegInfo:
    width: int
    height: int
    components: int
    sampling: tuple
    encoding: str
    markers: list = field(default_factory=list)

    @property
    def is_progressive(self) -> bool:
        return self.encoding == "progressive"

    @property
    def is_baseline(self) -> bool:
        return self.encoding == "baseline"


def read_jpeg(data: bytes) -> JpegInfo:
    """Пройти цепочку сегментов JPEG и вернуть параметры кадра.

    Именно это и делал `jpginfo.exe`, а также скрейп строки `Start Of Frame`
    из подробного вывода jpegtran (`iCatalyst.bat:776`, `:788`). Здесь ответ
    берётся из самого файла, поэтому не зависит ни от версии jpegtran, ни от
    языка его сообщений.
    """
    if not data.startswith(b"\xff\xd8"):
        raise ImageError("не JPEG: нет маркера SOI")
    pos = 2
    end = len(data)
    markers = []
    info = None
    while pos < end - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # заполнитель
            pos += 1
            continue
        if marker in _JPEG_STANDALONE:
            pos += 2
            continue
        if marker == 0xD9:  # EOI
            markers.append(marker)
            break
        if pos + 4 > end:
            raise ImageError("JPEG обрывается в заголовке сегмента")
        (seg_len,) = struct.unpack_from(">H", data, pos + 2)
        if seg_len < 2 or pos + 2 + seg_len > end:
            raise ImageError("JPEG: некорректная длина сегмента 0x%02X" % marker)
        markers.append(marker)
        if marker in _JPEG_SOF and info is None:
            body = data[pos + 4:pos + 2 + seg_len]
            if len(body) < 6:
                raise ImageError("JPEG: сегмент SOF слишком короткий")
            _prec, h, w, ncomp = struct.unpack_from(">BHHB", body, 0)
            sampling = []
            for i in range(ncomp):
                off = 6 + i * 3
                if off + 3 > len(body):
                    raise ImageError("JPEG: обрезанное описание компонент")
                sampling.append((body[off + 1] >> 4, body[off + 1] & 0x0F))
            info = JpegInfo(
                width=w, height=h, components=ncomp,
                sampling=tuple(sampling),
                encoding=_JPEG_SOF_NAMES.get(marker, "sof-0x%02x" % marker),
            )
        if marker == 0xDA:  # SOS — дальше идут энтропийные данные
            pos += 2 + seg_len
            # Пропускаем сжатые данные до следующего маркера, не являющегося
            # ни заполнителем, ни RSTn.
            while pos < end - 1:
                if data[pos] == 0xFF and data[pos + 1] not in (0x00, 0xFF) and \
                        not (0xD0 <= data[pos + 1] <= 0xD7):
                    break
                pos += 1
            continue
        pos += 2 + seg_len
    if info is None:
        raise ImageError("JPEG без сегмента SOF")
    info.markers = markers
    if 0xD9 not in markers:
        raise ImageError("JPEG без маркера EOI")
    return info


def jpeg_encoding(data: bytes) -> str:
    """Вернуть `baseline`, `progressive` или иное название процесса кодирования."""
    return read_jpeg(data).encoding


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

@dataclass
class GifFrame:
    left: int
    top: int
    width: int
    height: int
    delay: int
    disposal: int
    transparent: Optional[int]
    interlaced: bool
    indices: bytes
    palette: list


@dataclass
class GifInfo:
    width: int
    height: int
    frame_count: int
    loop_count: Optional[int]
    frames: list = field(default_factory=list)


def _lzw_decode(min_code_size: int, data: bytes, expected: int) -> bytearray:
    """Раскодировать поток LZW из GIF."""
    clear = 1 << min_code_size
    stop = clear + 1
    code_size = min_code_size + 1
    next_code = stop + 1
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    out = bytearray()
    prev = None
    bitpos = 0
    total_bits = len(data) * 8
    while bitpos + code_size <= total_bits:
        byte_at = bitpos >> 3
        chunk = data[byte_at:byte_at + 3]
        acc = int.from_bytes(chunk.ljust(3, b"\x00"), "little")
        code = (acc >> (bitpos & 7)) & ((1 << code_size) - 1)
        bitpos += code_size
        if code == clear:
            code_size = min_code_size + 1
            next_code = stop + 1
            table = table[:clear + 2]
            prev = None
            continue
        if code == stop:
            break
        if code < len(table) and (code < clear or table[code]):
            entry = table[code]
        elif prev is not None:
            entry = prev + prev[:1]
        else:
            raise ImageError("GIF: некорректный код LZW")
        out += entry
        if prev is not None:
            table.append(prev + entry[:1])
            next_code += 1
            if next_code >= (1 << code_size) and code_size < 12:
                code_size += 1
        prev = entry
        if len(out) >= expected:
            break
    return out


def _gif_deinterlace(indices: bytes, width: int, height: int) -> bytes:
    out = bytearray(len(indices))
    src = 0
    for y0, dy in ((0, 8), (4, 8), (2, 4), (1, 2)):
        for y in range(y0, height, dy):
            out[y * width:(y + 1) * width] = indices[src:src + width]
            src += width
    return bytes(out)


def read_gif(data: bytes, with_frames: bool = True) -> GifInfo:
    if data[:6] not in GIF_SIGNATURES:
        raise ImageError("не GIF: неверная подпись")
    if len(data) < 13:
        raise ImageError("GIF: обрезанный дескриптор экрана")
    width, height, packed = struct.unpack_from("<HHB", data, 6)
    pos = 13
    global_palette = []
    if packed & 0x80:
        n = 2 << (packed & 0x07)
        if pos + 3 * n > len(data):
            raise ImageError("GIF: обрезанная глобальная палитра")
        global_palette = [tuple(data[i:i + 3]) for i in range(pos, pos + 3 * n, 3)]
        pos += 3 * n

    def read_blocks(at: int) -> tuple:
        buf = bytearray()
        while at < len(data):
            size = data[at]
            at += 1
            if size == 0:
                return bytes(buf), at
            buf += data[at:at + size]
            at += size
        raise ImageError("GIF: незакрытая цепочка блоков")

    frames = []
    loop_count = None
    delay = 0
    disposal = 0
    transparent = None
    saw_trailer = False
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:  # trailer
            saw_trailer = True
            break
        if block == 0x21:  # extension
            if pos + 2 > len(data):
                raise ImageError("GIF: обрезанное расширение")
            label = data[pos + 1]
            payload, pos = read_blocks(pos + 2)
            if label == 0xF9 and len(payload) >= 4:
                flags = payload[0]
                disposal = (flags >> 2) & 0x07
                (delay,) = struct.unpack_from("<H", payload, 1)
                transparent = payload[3] if flags & 0x01 else None
            elif label == 0xFF and payload[:11] == b"NETSCAPE2.0" and len(payload) >= 14:
                # Счётчик циклов. `giftags=true` отдаёт gifsicle
                # `--no-extensions`, что выбрасывает это расширение и
                # превращает бесконечно циклящийся GIF в одноразовый.
                #
                # Смещение 12: read_blocks склеивает подблоки, поэтому payload
                # это 11 байт имени, байт идентификатора подблока 0x01 и два
                # байта счётчика.
                (loop_count,) = struct.unpack_from("<H", payload, 12)
            continue
        if block != 0x2C:
            raise ImageError("GIF: неизвестный блок 0x%02X" % block)
        if pos + 10 > len(data):
            raise ImageError("GIF: обрезанный дескриптор изображения")
        left, top, fw, fh, fpacked = struct.unpack_from("<HHHHB", data, pos + 1)
        pos += 10
        local_palette = global_palette
        if fpacked & 0x80:
            n = 2 << (fpacked & 0x07)
            local_palette = [tuple(data[i:i + 3]) for i in range(pos, pos + 3 * n, 3)]
            pos += 3 * n
        interlaced = bool(fpacked & 0x40)
        if pos >= len(data):
            raise ImageError("GIF: нет данных изображения")
        min_code_size = data[pos]
        payload, pos = read_blocks(pos + 1)
        indices = b""
        if with_frames:
            if not 2 <= min_code_size <= 11:
                raise ImageError("GIF: некорректный минимальный размер кода")
            raw = _lzw_decode(min_code_size, payload, fw * fh)
            if len(raw) < fw * fh:
                raise ImageError("GIF: кадр раскодирован не полностью")
            indices = bytes(raw[:fw * fh])
            if interlaced:
                indices = _gif_deinterlace(indices, fw, fh)
        frames.append(GifFrame(
            left=left, top=top, width=fw, height=fh, delay=delay,
            disposal=disposal, transparent=transparent, interlaced=interlaced,
            indices=indices, palette=local_palette,
        ))
        delay, disposal, transparent = 0, 0, None
    if not frames:
        raise ImageError("GIF без кадров")
    if not saw_trailer:
        raise ImageError("GIF без завершающего байта")
    return GifInfo(width, height, len(frames), loop_count, frames)


def gif_canvas_frames(data: bytes) -> list:
    """Собрать кадры GIF в полные RGBA-полотна.

    Сравнивать нужно именно полотна, а не сырые индексы: `gifsicle -O3`
    намеренно перекраивает кадры (меняет их размеры, смещения и прозрачность),
    сохраняя лишь то, что видит глаз. Индексы разойдутся, картинка — нет.
    """
    info = read_gif(data, with_frames=True)
    w, h = info.width, info.height
    canvas = bytearray(4 * w * h)
    result = []
    for frame in info.frames:
        before = bytes(canvas)
        for y in range(frame.height):
            cy = frame.top + y
            if cy >= h:
                break
            row = frame.indices[y * frame.width:(y + 1) * frame.width]
            for x, idx in enumerate(row):
                cx = frame.left + x
                if cx >= w:
                    break
                if frame.transparent is not None and idx == frame.transparent:
                    continue
                if idx >= len(frame.palette):
                    raise ImageError("GIF: индекс палитры вне диапазона")
                r, g, b = frame.palette[idx]
                at = 4 * (cy * w + cx)
                canvas[at] = r
                canvas[at + 1] = g
                canvas[at + 2] = b
                canvas[at + 3] = 255
        result.append((bytes(canvas), frame.delay))
        if frame.disposal == 2:  # восстановить фон
            for y in range(frame.height):
                cy = frame.top + y
                if cy >= h:
                    break
                at = 4 * (cy * w + frame.left)
                span = 4 * min(frame.width, w - frame.left)
                canvas[at:at + span] = bytes(span)
        elif frame.disposal == 3:  # восстановить предыдущее
            canvas = bytearray(before)
    return result


# ---------------------------------------------------------------------------
# Проверки, вызываемые конвейером
# ---------------------------------------------------------------------------

def validate(path, fmt: Optional[str] = None) -> str:
    """То же, что `validate_data`, но читает файл с диска."""
    with open(path, "rb") as fh:
        return validate_data(fh.read(), fmt)


def validate_data(data: bytes, fmt: Optional[str] = None) -> str:
    """Проверить, что данные — корректное изображение. Вернуть его формат.

    Бросает `ImageError` с человеческим описанием. Вызывается после каждой
    цепочки инструментов, чтобы «оптимизированный» огрызок не попал в вывод.
    """
    if not data:
        raise ImageError("файл пуст")
    actual = sniff(data)
    if actual is None:
        raise ImageError("формат не распознан")
    if fmt is not None and actual != fmt:
        raise ImageError("ожидался %s, а содержимое — %s" % (fmt, actual))
    if actual == "png":
        read_png(data)
    elif actual == "jpg":
        read_jpeg(data)
    else:
        read_gif(data, with_frames=True)
    return actual


def structure_equal(src_data: bytes, dst_data: bytes, fmt: str,
                    stripped: bool = False) -> Optional[str]:
    """Сравнить структуру до и после. Вернуть описание расхождения или None.

    Проверка дешёвая и включена всегда. Полное сравнение пикселей — отдельно:
    для GIF всегда, для PNG по `--verify`.
    """
    if fmt == "png":
        a, b = read_png(src_data), read_png(dst_data)
        if (a.width, a.height) != (b.width, b.height):
            return "размер изменился: %dx%d → %dx%d" % (a.width, a.height, b.width, b.height)
        if a.has_alpha and not b.has_alpha:
            return "потеряна прозрачность"
        lost = (a.chunk_names() & PNG_CRITICAL) - b.chunk_names()
        if lost - {b"PLTE"}:
            return "потерян критический чанк %s" % b", ".join(sorted(lost)).decode("latin-1")
        if not stripped:
            lost_render = (a.chunk_names() & PNG_RENDERING) - b.chunk_names()
            if lost_render:
                return "потерян чанк %s, а удаление метаданных не запрашивалось" % \
                    b", ".join(sorted(lost_render)).decode("latin-1")
        return None
    if fmt == "jpg":
        a, b = read_jpeg(src_data), read_jpeg(dst_data)
        if (a.width, a.height) != (b.width, b.height):
            return "размер изменился: %dx%d → %dx%d" % (a.width, a.height, b.width, b.height)
        if a.components != b.components:
            return "число компонент изменилось: %d → %d" % (a.components, b.components)
        if a.sampling != b.sampling:
            return "коэффициенты прореживания изменились: %r → %r" % (a.sampling, b.sampling)
        return None
    a, b = read_gif(src_data, with_frames=False), read_gif(dst_data, with_frames=False)
    if (a.width, a.height) != (b.width, b.height):
        return "размер изменился: %dx%d → %dx%d" % (a.width, a.height, b.width, b.height)
    if a.frame_count != b.frame_count:
        return "число кадров изменилось: %d → %d" % (a.frame_count, b.frame_count)
    if not stripped and a.loop_count != b.loop_count:
        return "счётчик циклов изменился: %r → %r" % (a.loop_count, b.loop_count)
    return None


def pixels_equal(src_data: bytes, dst_data: bytes, fmt: str,
                 allow_dirty_transparent: bool = False) -> Optional[str]:
    """Сравнить пиксели. Вернуть описание расхождения или None.

    `allow_dirty_transparent` включает сравнение «визуально идентично» вместо
    «побитово идентично»: альфа должна совпадать везде, а RGB — только там, где
    альфа не ноль. Именно это разрешает `xtreme=/a1` в config.ini, где TruePNG
    переписывает RGB под полностью прозрачными пикселями.
    """
    if fmt == "png":
        aw, ah, apx = png_pixels(src_data)
        bw, bh, bpx = png_pixels(dst_data)
        if (aw, ah) != (bw, bh):
            return "размер изменился: %dx%d → %dx%d" % (aw, ah, bw, bh)
        if apx == bpx:
            return None
        for i in range(0, len(apx), 4):
            if apx[i + 3] != bpx[i + 3]:
                px = i // 4
                return "альфа отличается в пикселе (%d, %d)" % (px % aw, px // aw)
            if allow_dirty_transparent and apx[i + 3] == 0:
                continue
            if apx[i:i + 3] != bpx[i:i + 3]:
                px = i // 4
                return "цвет отличается в пикселе (%d, %d)" % (px % aw, px // aw)
        return None
    if fmt == "gif":
        a = gif_canvas_frames(src_data)
        b = gif_canvas_frames(dst_data)
        if len(a) != len(b):
            return "число кадров изменилось: %d → %d" % (len(a), len(b))
        for i, ((ca, da), (cb, db)) in enumerate(zip(a, b)):
            if ca != cb:
                return "кадр %d отличается по пикселям" % i
            if da != db:
                return "задержка кадра %d изменилась: %d → %d" % (i, da, db)
        return None
    # Для JPEG побитовое равенство коэффициентов DCT гарантирует сам jpegtran:
    # он выполняет только преобразования в энтропийной области. Декодировать
    # JPEG здесь нечем, поэтому ограничиваемся структурной проверкой.
    return None
