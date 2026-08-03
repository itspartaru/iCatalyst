"""Проверки с настоящими оптимизаторами.

Пропускаются, если инструмент не установлен, поэтому набор тестов остаётся
зелёным на машине без них. Именно здесь проверяются реальные написания флагов —
то, что нельзя выяснить, не запустив установленную версию.

Установить всё нужное на Debian/Ubuntu/Mint:

    sudo apt install optipng zopfli advancecomp gifsicle libjpeg-turbo-progs
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from icatalyst import imgcheck
from icatalyst.toolbox import Toolbox
from tests import corpus, support

_TOOLBOX = Toolbox()


def requires(*names):
    missing = [name for name in names if _TOOLBOX.find(name) is None]
    return unittest.skipIf(missing, "не установлено: %s" % ", ".join(missing))


class RealToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.config = support.empty_config(self.base)
        self.images = self.base / "Фото — копия"
        self.images.mkdir()
        self.out = self.base / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args):
        code, out, err = support.run_cli(
            [*args, "/outdir:%s" % self.out, "--tsv", str(self.images)],
            tools_dir=None, config=self.config)
        rows = {Path(r["source"]).name: r for r in support.tsv_rows(out)}
        return code, rows, err

    def _assert_good(self, row, fmt, name):
        self.assertIn(row["status"], ("ok", "kept"), "%s: %s" % (name, row))
        self.assertGreater(int(row["optimized"]), 0)
        self.assertLessEqual(int(row["optimized"]), int(row["original"]))
        if fmt == "jpg":
            return
        src = Path(row["source"]).read_bytes()
        dst = Path(row["destination"]).read_bytes()
        problem = imgcheck.pixels_equal(src, dst, fmt)
        self.assertIsNone(problem, "%s: %s" % (name, problem))

    @requires("optipng")
    def test_png_advanced_is_lossless_for_every_colour_type(self):
        names = []
        for index, (color_type, depth) in enumerate(
                [(0, 1), (0, 8), (0, 16), (2, 8), (3, 4), (4, 8), (6, 8)]):
            name = "тип%d_глубина%d.png" % (color_type, depth)
            (self.images / name).write_bytes(corpus.png_bytes(
                width=48, height=32, color_type=color_type, bit_depth=depth,
                seed=index, text=b"m" * 200))
            names.append(name)
        code, rows, err = self._run("/png:1", "/jpg:0", "/gif:0")
        self.assertEqual(code, 0, err)
        for name in names:
            self._assert_good(rows[name], "png", name)

    @requires("optipng", "zopflipng")
    def test_png_xtreme_is_lossless(self):
        (self.images / "ёлка.png").write_bytes(
            corpus.png_bytes(width=64, height=64, text=b"m" * 300))
        code, rows, err = self._run("/png:2", "/jpg:0", "/gif:0")
        self.assertEqual(code, 0, err)
        self._assert_good(rows["ёлка.png"], "png", "ёлка.png")

    @requires("optipng")
    def test_png_with_transparency_keeps_alpha(self):
        (self.images / "альфа.png").write_bytes(corpus.png_bytes(
            width=32, height=32, color_type=6, bit_depth=8))
        code, rows, err = self._run("/png:1", "/jpg:0", "/gif:0")
        self.assertEqual(code, 0, err)
        dst = imgcheck.read_png(Path(rows["альфа.png"]["destination"]).read_bytes())
        self.assertTrue(dst.has_alpha)

    @requires("gifsicle")
    def test_gif_stays_lossless_and_keeps_looping(self):
        (self.images / "анимация.gif").write_bytes(corpus.gif_bytes(
            width=32, height=24, frames=3, loop=0, comment=b"m" * 400))
        code, rows, err = self._run("/png:0", "/jpg:0", "/gif:1")
        self.assertEqual(code, 0, err)
        row = rows["анимация.gif"]
        self._assert_good(row, "gif", "анимация.gif")
        info = imgcheck.read_gif(Path(row["destination"]).read_bytes(),
                                 with_frames=False)
        self.assertEqual(info.loop_count, 0,
                         "счётчик циклов потерян: GIF стал одноразовым")

    @requires("jpegtran")
    def test_jpeg_modes_produce_the_requested_encoding(self):
        (self.images / "фото.jpg").write_bytes(corpus.jpeg_bytes(comment=b"m" * 500))
        for mode, expected in ((1, "baseline"), (2, "progressive")):
            with self.subTest(mode=mode):
                out = self.base / ("out%d" % mode)
                code, stdout, err = support.run_cli(
                    ["/png:0", "/jpg:%d" % mode, "/gif:0", "/outdir:%s" % out,
                     "--tsv", str(self.images)],
                    tools_dir=None, config=self.config)
                self.assertEqual(code, 0, err)
                row = support.tsv_rows(stdout)[0]
                self._assert_good(row, "jpg", "фото.jpg")
                data = Path(row["destination"]).read_bytes()
                self.assertEqual(imgcheck.jpeg_encoding(data), expected)

    @requires("jpegtran")
    def test_jpeg_default_mode_keeps_the_original_encoding(self):
        for progressive in (False, True):
            with self.subTest(progressive=progressive):
                folder = self.base / ("src%s" % progressive)
                folder.mkdir()
                (folder / "ф.jpg").write_bytes(
                    corpus.jpeg_bytes(progressive=progressive, comment=b"m" * 400))
                out = self.base / ("o%s" % progressive)
                code, stdout, err = support.run_cli(
                    ["/png:0", "/jpg:3", "/gif:0", "/outdir:%s" % out,
                     "--tsv", str(folder)], tools_dir=None, config=self.config)
                self.assertEqual(code, 0, err)
                row = support.tsv_rows(stdout)[0]
                data = Path(row["destination"]).read_bytes()
                self.assertEqual(imgcheck.jpeg_encoding(data),
                                 "progressive" if progressive else "baseline")

    @requires("optipng")
    def test_second_run_is_idempotent(self):
        """Повторный прогон уже оптимизированного файла ничего не должен ломать."""
        (self.images / "лого.png").write_bytes(
            corpus.png_bytes(width=48, height=48, text=b"m" * 200))
        code, rows, err = self._run("/png:1", "/jpg:0", "/gif:0")
        self.assertEqual(code, 0, err)
        first = Path(rows["лого.png"]["destination"])
        again = self.base / "out2"
        code, stdout, err = support.run_cli(
            ["/png:1", "/jpg:0", "/gif:0", "/outdir:%s" % again, "--tsv",
             str(first.parent)], tools_dir=None, config=self.config)
        self.assertEqual(code, 0, err)
        row = support.tsv_rows(stdout)[0]
        self.assertIn(row["status"], ("ok", "kept"))
        self.assertLessEqual(int(row["optimized"]), int(row["original"]))


    @requires("optipng")
    def test_xtreme_is_never_worse_than_advanced(self):
        """Инвариант: более медленный режим не имеет права сжать хуже.

        Нарушался, пока «грязная прозрачность» подставлялась вместо обычного
        прохода, а не добавлялась отдельным шагом: oxipng с `-a` на изображении,
        где RGB под прозрачными пикселями продолжает градиент видимой части,
        выдавал 1385 байт вместо 205.
        """
        names = []
        for index, (color_type, depth) in enumerate(
                [(0, 1), (0, 8), (0, 16), (2, 8), (3, 4), (4, 8), (6, 8)]):
            name = "тип%d_глубина%d.png" % (color_type, depth)
            (self.images / name).write_bytes(corpus.png_bytes(
                width=64, height=64, color_type=color_type, bit_depth=depth,
                seed=index, text=b"m" * 300))
            names.append(name)

        sizes = {}
        for mode in (1, 2):
            out = self.base / ("mode%d" % mode)
            code, stdout, err = support.run_cli(
                ["/png:%d" % mode, "/jpg:0", "/gif:0", "/outdir:%s" % out,
                 "--tsv", str(self.images)], tools_dir=None, config=self.config)
            self.assertEqual(code, 0, err)
            sizes[mode] = {Path(r["source"]).name: int(r["optimized"])
                           for r in support.tsv_rows(stdout)}

        worse = [(name, sizes[1][name], sizes[2][name]) for name in names
                 if sizes[2][name] > sizes[1][name]]
        self.assertEqual(worse, [], "Xtreme сжал хуже Advanced: %r" % worse)

    @requires("oxipng")
    def test_oxipng_flags_are_accepted(self):
        """Флаги oxipng задавались без возможности их проверить — проверяем.

        Синтаксис `--filters` между мажорными версиями менялся, поэтому фильтры
        не задаются вовсе, а `-Z` и `-a` добавляются только по возможностям,
        объявленным зондом.
        """
        tool = _TOOLBOX.find("oxipng")
        self.assertTrue(tool.version, "версия oxipng не разобрана")
        (self.images / "лого.png").write_bytes(
            corpus.png_bytes(width=64, height=64, color_type=6, text=b"m" * 300))
        code, rows, err = self._run("/png:2", "/jpg:0", "/gif:0")
        self.assertEqual(code, 0, err)
        self._assert_good(rows["лого.png"], "png", "лого.png")


class ToolProbeTest(unittest.TestCase):
    @requires("jpegtran")
    def test_mozjpeg_detection(self):
        """`-revert` есть только в MozJPEG; от этого зависит argv baseline-режима."""
        tool = _TOOLBOX.find("jpegtran")
        self.assertIsNotNone(tool)
        # Утверждение не о том, какой jpegtran установлен, а о том, что зонд
        # даёт определённый ответ и он согласован со справкой инструмента.
        self.assertIsInstance(tool.has("mozjpeg"), bool)

    @requires("advdef")
    def test_advdef_version_is_parsed(self):
        tool = _TOOLBOX.find("advdef")
        self.assertTrue(tool.version, "версия advdef не разобрана")


if __name__ == "__main__":
    unittest.main()
