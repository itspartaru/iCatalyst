"""Свойства конвейера, проверенные поддельными инструментами.

Эти тесты — причина, по которой поддельные оптимизаторы вообще существуют: они
проходят на машине, где не установлено ни одного настоящего оптимизатора, и
закрепляют главное обещание программы — результат либо строго меньше входа и
без потерь, либо оригинал остаётся нетронутым.
"""

from __future__ import annotations

import filecmp
import shutil
import tempfile
import unittest
from pathlib import Path

from icatalyst import imgcheck
from tests import corpus, support


class ScenarioTest(unittest.TestCase):
    """Базовый класс: каталог с тремя файлами, поддельные инструменты, вывод."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = support.empty_config(self.base)
        self.images = self.base / "Изображения"
        self.images.mkdir()
        self.png = self.images / "лого.png"
        self.jpg = self.images / "фото.jpg"
        self.gif = self.images / "анимация.gif"
        self.png.write_bytes(corpus.png_bytes(width=32, height=32, text=b"m" * 300))
        self.jpg.write_bytes(corpus.jpeg_bytes(comment=b"m" * 400))
        self.gif.write_bytes(corpus.gif_bytes(width=24, height=16, frames=2,
                                             loop=0, comment=b"m" * 400))
        self.originals = {p: p.read_bytes() for p in (self.png, self.jpg, self.gif)}
        self.out = self.base / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def run_modes(self, *extra, mode: str = None, target: str = None,
                  in_place: bool = False, tools=None, **env):
        args = ["/png:1", "/jpg:1", "/gif:1",
                "/outdir:false" if in_place else "/outdir:%s" % self.out,
                "--tsv", str(self.images), *extra]
        if mode:
            env["ICATALYST_FAKE_MODE"] = mode
        if target:
            env["ICATALYST_FAKE_TARGET"] = target
        code, out, err = support.run_cli(args, tools_dir=tools or self.tools,
                                         config=self.config, **env)
        return code, {Path(r["source"]).name: r for r in support.tsv_rows(out)}, err

    def single_chain_tools(self):
        """Набор без oxipng: PNG остаётся с единственной цепочкой.

        Нужно там, где проверяется полный провал формата: при доступном oxipng
        гонка подхватила бы второго кандидата, и проверять было бы нечего.
        """
        return support.install_fake_tools(
            self.base / "single", only=["optipng", "advdef", "zopflipng",
                                        "gifsicle", "jpegtran"])

    def assertOriginalsIntact(self):
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data,
                             "оригинал %s изменился" % path.name)


class HappyPathTest(ScenarioTest):
    def test_every_format_shrinks_and_stays_lossless(self):
        code, rows, err = self.run_modes()
        self.assertEqual(code, 0, err)
        self.assertEqual(set(rows), {"лого.png", "фото.jpg", "анимация.gif"})
        for name, row in rows.items():
            self.assertEqual(row["status"], "ok", "%s: %s" % (name, row))
            self.assertLess(int(row["optimized"]), int(row["original"]))
            self.assertGreater(int(row["optimized"]), 0)
            self.assertTrue(Path(row["destination"]).is_file())
        self.assertOriginalsIntact()

    def test_output_tree_mirrors_input(self):
        self.run_modes()
        self.assertTrue((self.out / "Изображения" / "лого.png").is_file())

    def test_pixels_are_unchanged(self):
        _, rows, _ = self.run_modes()
        for name, fmt in (("лого.png", "png"), ("анимация.gif", "gif")):
            src = self.originals[self.images / name]
            dst = Path(rows[name]["destination"]).read_bytes()
            self.assertIsNone(imgcheck.pixels_equal(src, dst, fmt),
                              "%s изменился по пикселям" % name)

    def test_gif_loop_count_survives_metadata_removal(self):
        """`--no-extensions` в 2.7 делал циклящийся GIF одноразовым."""
        _, rows, _ = self.run_modes()
        dst = Path(rows["анимация.gif"]["destination"]).read_bytes()
        self.assertEqual(imgcheck.read_gif(dst, with_frames=False).loop_count, 0)

    def test_xtreme_mode_uses_the_second_chain(self):
        code, rows, err = self.run_modes(*[], **{})
        self.assertEqual(code, 0, err)
        args = ["/png:2", "/jpg:0", "/gif:0", "/outdir:%s" % self.out,
                "--tsv", str(self.images)]
        code, out, err = support.run_cli(args, tools_dir=self.tools,
                                         config=self.config)
        self.assertEqual(code, 0, err)
        rows = {Path(r["source"]).name: r for r in support.tsv_rows(out)}
        self.assertEqual(rows["лого.png"]["status"], "ok")
        self.assertIn("zopflipng", rows["лого.png"]["chain"])

    def test_in_place_replaces_the_original(self):
        code, rows, err = self.run_modes(in_place=True)
        self.assertEqual(code, 0, err)
        for name in rows:
            row = rows[name]
            self.assertEqual(Path(row["destination"]), self.images / name)
            self.assertEqual(row["status"], "ok")
            self.assertLess((self.images / name).stat().st_size,
                            len(self.originals[self.images / name]))
        # Пиксели должны сохраниться и при перезаписи на месте.
        self.assertIsNone(imgcheck.pixels_equal(
            self.originals[self.png], self.png.read_bytes(), "png"))


class RatchetTest(ScenarioTest):
    """Ограничитель размера D2: результат не может оказаться хуже входа."""

    def test_grown_result_is_rejected(self):
        code, rows, err = self.run_modes(mode="grow")
        self.assertEqual(code, 0, err)
        for name, row in rows.items():
            self.assertEqual(row["status"], "kept", "%s: %s" % (name, row))
            self.assertEqual(row["optimized"], row["original"])
            # При выводе в каталог оригинал всё равно копируется — так делал
            # `:backup2`, и файл в назначении обязан быть байт в байт исходным.
            self.assertTrue(filecmp.cmp(str(self.images / name),
                                        row["destination"], shallow=False))
        self.assertOriginalsIntact()

    def test_equal_result_is_rejected(self):
        _, rows, _ = self.run_modes(mode="equal")
        for row in rows.values():
            self.assertEqual(row["status"], "kept")
        self.assertOriginalsIntact()

    def test_zero_length_result_is_a_failure(self):
        code, rows, _ = self.run_modes(mode="zero")
        self.assertEqual(code, 1)
        for row in rows.values():
            self.assertEqual(row["status"], "failed")
        self.assertOriginalsIntact()

    def test_garbage_result_is_rejected_by_validation(self):
        code, rows, _ = self.run_modes(mode="garbage")
        self.assertEqual(code, 1)
        for row in rows.values():
            self.assertEqual(row["status"], "failed")
        self.assertOriginalsIntact()

    def test_nonzero_exit_is_a_failure(self):
        code, rows, _ = self.run_modes(mode="fail")
        self.assertEqual(code, 1)
        for row in rows.values():
            self.assertEqual(row["status"], "failed")
        self.assertOriginalsIntact()

    def test_in_place_survives_a_failing_tool(self):
        """Провал не должен оставить на месте оригинала обрезанный файл."""
        code, rows, _ = self.run_modes(mode="zero", in_place=True)
        self.assertEqual(code, 1)
        self.assertOriginalsIntact()

    def test_optional_step_failure_does_not_lose_the_file(self):
        """advdef необязателен: его падение не должно ронять файл."""
        code, rows, err = self.run_modes(mode="fail", target="advdef")
        self.assertEqual(code, 0, err)
        self.assertEqual(rows["лого.png"]["status"], "ok")

    def test_required_step_failure_is_reported(self):
        """optipng обязателен: без него единственная PNG-цепочка не отработает."""
        code, rows, _ = self.run_modes(mode="fail", target="optipng",
                                       tools=self.single_chain_tools())
        self.assertEqual(rows["лого.png"]["status"], "failed")
        # Остальные форматы не задеты.
        self.assertEqual(rows["фото.jpg"]["status"], "ok")

    def test_race_rescues_the_file_when_optipng_fails(self):
        """С доступным oxipng тот же сбой файл уже не теряет."""
        code, rows, err = self.run_modes(mode="fail", target="optipng")
        self.assertEqual(code, 0, err)
        self.assertEqual(rows["лого.png"]["status"], "ok")
        self.assertEqual(rows["лого.png"]["chain"], "oxipng")


class StrictLosslessTest(ScenarioTest):
    def test_flag_changes_the_command_not_just_the_check(self):
        """`--strict-lossless` обязан убрать `--lossy_transparent` из вызова.

        Раньше флаг влиял только на сравнение результата, поэтому обещание
        «не менять RGB под полностью прозрачными пикселями» не выполнялось.
        """
        code, out, err = support.run_cli(
            ["--doctor"], tools_dir=self.tools, config=self.config)
        self.assertIn("--lossy_transparent", out, err)

        code, out, err = support.run_cli(
            ["--doctor", "--strict-lossless"], tools_dir=self.tools,
            config=self.config)
        self.assertEqual(code, 0, err)
        self.assertNotIn("--lossy_transparent", out)

    def test_windows_flags_are_rerendered(self):
        """На Windows тот же флаг должен превращать `/a1` в `/a0` для TruePNG."""
        from icatalyst import config as cfgmod
        opts = cfgmod.parse_truepng_flags("/a1 /g0")
        self.assertTrue(opts.dirty_transparency)
        opts.dirty_transparency = False
        self.assertEqual(cfgmod.render_truepng_flags(opts), ("/a0", "/g0"))


class TimeoutTest(ScenarioTest):
    def test_hanging_tool_is_killed(self):
        config = self.base / "timeout.ini"
        config.write_text("[options]\ntimeout=1\n", encoding="utf-8")
        args = ["/png:1", "/jpg:0", "/gif:0", "/outdir:%s" % self.out,
                "--tsv", str(self.images)]
        code, out, err = support.run_cli(
            args, tools_dir=self.single_chain_tools(), config=config,
            ICATALYST_FAKE_MODE="hang", ICATALYST_FAKE_TARGET="optipng")
        rows = {Path(r["source"]).name: r for r in support.tsv_rows(out)}
        self.assertEqual(rows["лого.png"]["status"], "failed")
        self.assertOriginalsIntact()


class MissingToolTest(ScenarioTest):
    def test_absent_required_tool_is_reported_once(self):
        only = support.install_fake_tools(self.base / "partial", only=["jpegtran"])
        args = ["/png:1", "/jpg:1", "/gif:0", "/outdir:%s" % self.out,
                str(self.images)]
        code, out, err = support.run_cli(args, tools_dir=only, config=self.config)
        self.assertIn("optipng", out + err)
        # Подсказка про apt должна быть, и ровно одна на прогон.
        self.assertEqual((out + err).count("sudo apt install optipng"), 1)


if __name__ == "__main__":
    unittest.main()
