"""Проверки разбора форматов — заменителя `jpginfo.exe` и основы проверок."""

from __future__ import annotations

import unittest
import zlib

from icatalyst import imgcheck
from tests import corpus


class SniffTest(unittest.TestCase):
    def test_detects_all_three_formats(self):
        self.assertEqual(imgcheck.sniff(corpus.png_bytes()), "png")
        self.assertEqual(imgcheck.sniff(corpus.jpeg_bytes()), "jpg")
        self.assertEqual(imgcheck.sniff(corpus.gif_bytes()), "gif")

    def test_rejects_non_images(self):
        self.assertIsNone(imgcheck.sniff(b"just text"))
        self.assertIsNone(imgcheck.sniff(b""))


class PngTest(unittest.TestCase):
    def test_reads_every_colour_type(self):
        cases = [(0, 1), (0, 8), (0, 16), (2, 8), (3, 4), (4, 8), (6, 8)]
        for color_type, depth in cases:
            data = corpus.png_bytes(color_type=color_type, bit_depth=depth)
            info = imgcheck.read_png(data)
            self.assertEqual((info.width, info.height), (8, 8))
            self.assertEqual(info.color_type, color_type)
            self.assertEqual(info.bit_depth, depth)

    def test_crc_error_is_reported(self):
        data = bytearray(corpus.png_bytes())
        # Портим последний байт CRC чанка IHDR.
        data[29] ^= 0xFF
        with self.assertRaises(imgcheck.ImageError):
            imgcheck.read_png(bytes(data))

    def test_recompression_preserves_pixels(self):
        """Пересжатие IDAT другим уровнем zlib не меняет ни один пиксель."""
        for color_type, depth in [(0, 1), (3, 4), (6, 8), (0, 16)]:
            # Размер нарочно не 8x8: на десятке байт «без сжатия» и «уровень 9»
            # дают одинаковую длину, и проверка была бы бессодержательной.
            kwargs = dict(width=48, height=48, color_type=color_type, bit_depth=depth)
            loose = corpus.png_bytes(level=0, **kwargs)
            tight = corpus.png_bytes(level=9, **kwargs)
            self.assertLess(len(tight), len(loose))
            self.assertIsNone(imgcheck.pixels_equal(loose, tight, "png"))

    def test_pixel_difference_is_found(self):
        original = corpus.png_bytes(color_type=6, bit_depth=8, seed=0)
        other = corpus.png_bytes(color_type=6, bit_depth=8, seed=1)
        self.assertIsNotNone(imgcheck.pixels_equal(original, other, "png"))

    def test_dirty_transparency_is_visible_only_in_strict_mode(self):
        """RGB под полностью прозрачными пикселями — это `/a1` из config.ini."""
        width = height = 4
        base = bytearray()
        dirty = bytearray()
        for y in range(height):
            base.append(0)
            dirty.append(0)
            for x in range(width):
                opaque = (x + y) % 2
                alpha = 255 if opaque else 0
                base += bytes((10, 20, 30, alpha))
                # Под прозрачными пикселями цвет другой.
                dirty += bytes((10, 20, 30, alpha) if opaque else (0, 0, 0, 0))
        def build(raw):
            import struct
            out = [imgcheck.PNG_SIGNATURE]
            ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
            for name, payload in ((b"IHDR", ihdr),
                                  (b"IDAT", zlib.compress(bytes(raw), 9)),
                                  (b"IEND", b"")):
                out.append(struct.pack(">I", len(payload)) + name + payload
                           + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF))
            return b"".join(out)

        a, b = build(base), build(dirty)
        self.assertIsNone(imgcheck.pixels_equal(a, b, "png",
                                                allow_dirty_transparent=True))
        self.assertIsNotNone(imgcheck.pixels_equal(a, b, "png",
                                                   allow_dirty_transparent=False))


class JpegTest(unittest.TestCase):
    def test_encoding_detection(self):
        self.assertEqual(imgcheck.jpeg_encoding(corpus.jpeg_bytes(False)), "baseline")
        self.assertEqual(imgcheck.jpeg_encoding(corpus.jpeg_bytes(True)), "progressive")

    def test_geometry_and_components(self):
        info = imgcheck.read_jpeg(corpus.jpeg_bytes())
        self.assertEqual((info.width, info.height), (8, 8))
        self.assertEqual(info.components, 1)
        self.assertEqual(info.sampling, ((1, 1),))

    def test_missing_eoi_is_rejected(self):
        data = corpus.jpeg_bytes()[:-2]
        with self.assertRaises(imgcheck.ImageError):
            imgcheck.read_jpeg(data)

    def test_comment_removal_keeps_structure(self):
        fat = corpus.jpeg_bytes(comment=b"z" * 500)
        lean = corpus.jpeg_bytes(comment=None)
        self.assertGreater(len(fat), len(lean))
        self.assertIsNone(imgcheck.structure_equal(fat, lean, "jpg", stripped=True))


class GifTest(unittest.TestCase):
    def test_loop_count_and_frames(self):
        data = corpus.gif_bytes(frames=3, loop=0)
        info = imgcheck.read_gif(data)
        self.assertEqual(info.frame_count, 3)
        self.assertEqual(info.loop_count, 0)

    def test_absent_loop_extension(self):
        info = imgcheck.read_gif(corpus.gif_bytes(loop=None))
        self.assertIsNone(info.loop_count)

    def test_lzw_roundtrip(self):
        """Наш кодировщик и декодер LZW согласованы между собой."""
        info = imgcheck.read_gif(corpus.gif_bytes(width=16, height=9, frames=2))
        for frame in info.frames:
            self.assertEqual(len(frame.indices), frame.width * frame.height)
            expected = bytes(((x + y + info.frames.index(frame)) % 4)
                             for y in range(9) for x in range(16))
            self.assertEqual(frame.indices, expected)

    def test_comment_removal_keeps_pixels(self):
        fat = corpus.gif_bytes(comment=b"c" * 400)
        lean = corpus.gif_bytes(comment=None)
        self.assertGreater(len(fat), len(lean))
        self.assertIsNone(imgcheck.pixels_equal(fat, lean, "gif"))

    def test_losing_loop_extension_is_reported(self):
        looping = corpus.gif_bytes(loop=0)
        once = corpus.gif_bytes(loop=None)
        problem = imgcheck.structure_equal(looping, once, "gif", stripped=False)
        self.assertIsNotNone(problem)
        self.assertIn("цикл", problem)


class ValidateTest(unittest.TestCase):
    def test_rejects_empty_truncated_and_garbage(self):
        for data in (b"", corpus.png_bytes()[:40], b"nonsense"):
            with self.assertRaises(imgcheck.ImageError):
                imgcheck.validate_data(data)

    def test_format_mismatch_is_reported(self):
        with self.assertRaises(imgcheck.ImageError):
            imgcheck.validate_data(corpus.png_bytes(), "jpg")

    def test_lost_rendering_chunk_only_matters_without_stripping(self):
        with_gama = corpus.png_bytes(text=b"hello")
        # Собираем тот же PNG без gAMA, вырезав чанк.
        import struct
        stripped = bytearray(with_gama[:8])
        pos = 8
        while pos < len(with_gama):
            (length,) = struct.unpack_from(">I", with_gama, pos)
            name = with_gama[pos + 4:pos + 8]
            end = pos + 12 + length
            if name != b"gAMA":
                stripped += with_gama[pos:end]
            pos = end
        stripped = bytes(stripped)
        self.assertIsNotNone(
            imgcheck.structure_equal(with_gama, stripped, "png", stripped=False))
        self.assertIsNone(
            imgcheck.structure_equal(with_gama, stripped, "png", stripped=True))


if __name__ == "__main__":
    unittest.main()
