"""Сквозные проверки: главная из них — что исходный баг мёртв.

Под кодом 2.7 каждое имя из `tests/corpus.py` либо попадало в корзину «Images
with characters», либо, что хуже, молча исчезало в «Images are not found».
Утверждение «каждый входной файл присутствует в выводе, и корзина испорченных
имён пуста» и есть регрессионный тест на этот баг.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from icatalyst import imgcheck, scan
from tests import corpus, support

REPO_ROOT = Path(__file__).resolve().parent.parent


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.tools = support.install_fake_tools(cls.base / "fakebin")
        cls.config = support.empty_config(cls.base)
        cls.images = cls.base / "корпус"
        cls.images.mkdir()
        corpus.build_corpus(cls.images)
        cls.out = cls.base / "результат"
        # Слепок исходников: ни один из них не должен измениться при выводе
        # в отдельный каталог.
        cls.before = {p: p.stat().st_size for p in _all_files(cls.images)}
        cls.code, stdout, cls.stderr = support.run_cli(
            ["/png:1", "/jpg:3", "/gif:1", "/outdir:%s" % cls.out, "--tsv",
             str(cls.images)],
            tools_dir=cls.tools, config=cls.config)
        cls.rows = support.tsv_rows(stdout)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_recognised_file_is_reported(self):
        """Ни один файл не пропадает молча — это и есть исходный баг."""
        expected = {str(p) for p in _all_files(self.images)
                    if scan.classify(p.name)}
        reported = {row["source"] for row in self.rows}
        self.assertEqual(reported, expected)
        self.assertGreater(len(expected), 40, "корпус подозрительно мал")

    def test_cyrillic_and_punctuation_survive(self):
        """Имена, на которых 2.7 спотыкался, обработаны нормально."""
        by_name = {Path(row["source"]).name: row for row in self.rows}
        for name in ("лого.png", "Ёлка.PNG", "photo (1).jpg", "photo & co.jpeg",
                     "100%.jpe", "анимация.gif", "Шишкин.png", "emoji🎉.png",
                     "Ім’я—файл….jpg", "spaces   inside.png"):
            self.assertIn(name, by_name, "имя %r не дошло до отчёта" % name)
            self.assertIn(by_name[name]["status"], ("ok", "kept"),
                          "имя %r обработано со статусом %s"
                          % (name, by_name[name]["status"]))

    def test_directories_with_hostile_names_are_traversed(self):
        parents = {Path(row["source"]).parent.name for row in self.rows}
        for name in ("Тест — тире", "«Кавычки»", "Ґуля і Їжак", "Школа",
                     "(parens)", "!bang!", "100%percent", "emoji🎨🔥",
                     "CJK日本語漢字"):
            self.assertIn(name, parents, "каталог %r не обойдён" % name)

    def test_long_paths_are_handled(self):
        deep = [row for row in self.rows if len(row["source"]) > 260]
        if not deep:
            # На Windows создание такого пути зависит от системной настройки
            # LongPathsEnabled, и генератор корпуса молча пропускает то, чего
            # файловая система не дала создать. На POSIX оправданий нет.
            if os.name == "nt":
                self.skipTest("файловая система не дала создать путь длиннее 260")
            self.fail("в корпусе нет путей длиннее 260 символов")
        for row in deep:
            self.assertIn(row["status"], ("ok", "kept"), row)

    def test_only_deliberately_broken_files_fail(self):
        bad = {Path(row["source"]).name for row in self.rows
               if row["status"] not in ("ok", "kept")}
        self.assertEqual(bad, {"broken.png", "truncated.png", "actually_a_png.jpg"})

    def test_nothing_ever_grows(self):
        for row in self.rows:
            if row["status"] not in ("ok", "kept"):
                continue
            original, optimized = int(row["original"]), int(row["optimized"])
            self.assertGreater(optimized, 0, row)
            self.assertLessEqual(optimized, original, row)

    def test_sources_are_untouched(self):
        for path, size in self.before.items():
            self.assertTrue(path.exists(), "исходник %s исчез" % path)
            self.assertEqual(path.stat().st_size, size,
                             "исходник %s изменился" % path)

    def test_output_tree_mirrors_input_tree(self):
        produced = {row["destination"] for row in self.rows if row["destination"]}
        for destination in produced:
            self.assertTrue(Path(destination).is_file(),
                            "файл %s не создан" % destination)
        relative_in = {str(Path(row["source"]).relative_to(self.images))
                       for row in self.rows if row["destination"]}
        relative_out = {str(Path(row["destination"]).relative_to(self.out / "корпус"))
                        for row in self.rows if row["destination"]}
        self.assertEqual(relative_in, relative_out)

    def test_optimisation_is_lossless(self):
        for row in self.rows:
            if row["status"] != "ok":
                continue
            fmt = row["format"]
            if fmt == "jpg":
                continue  # jpegtran не меняет коэффициенты по построению
            src = Path(row["source"]).read_bytes()
            dst = Path(row["destination"]).read_bytes()
            problem = imgcheck.pixels_equal(src, dst, fmt)
            self.assertIsNone(problem, "%s: %s" % (row["source"], problem))

    def test_no_bad_characters_bucket(self):
        """Корзина «Images with characters» из 2.7 обязана быть пуста всегда."""
        self.assertNotIn("Images with characters", self.stderr)


class ArgumentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = support.empty_config(self.base)
        self.images = self.base / "img"
        self.images.mkdir()
        (self.images / "a.png").write_bytes(corpus.png_bytes(width=32, height=32))

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_arguments_prints_the_manual(self):
        code, out, _ = support.run_cli([], config=self.config)
        self.assertEqual(code, 0)
        self.assertIn("Usage: icatalyst", out)
        self.assertIn("/png:#", out)

    def test_unknown_option_is_rejected(self):
        code, _, err = support.run_cli(["/nonsense:1", str(self.images)],
                                       config=self.config)
        self.assertEqual(code, 2)
        self.assertIn("nonsense", err)

    def test_mode_out_of_range_is_rejected(self):
        code, _, err = support.run_cli(["/png:9", str(self.images)],
                                       config=self.config)
        self.assertEqual(code, 2)
        self.assertIn("0..2", err)

    def test_legacy_keys_are_case_insensitive(self):
        code, out, err = support.run_cli(
            ["/PNG:1", "/OutDir:%s" % (self.base / "o"), "--tsv", str(self.images)],
            tools_dir=self.tools, config=self.config)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(support.tsv_rows(out)), 1)

    def test_modern_flag_form_works_too(self):
        code, out, err = support.run_cli(
            ["--png=1", "--outdir=%s" % (self.base / "o2"), "--tsv", str(self.images)],
            tools_dir=self.tools, config=self.config)
        self.assertEqual(code, 0, err)

    def test_missing_mode_without_a_terminal_is_an_error(self):
        """Без терминала меню показать нельзя, и молча пропускать формат нельзя."""
        code, _, err = support.run_cli(
            ["/outdir:false", str(self.images)], tools_dir=self.tools,
            config=self.config)
        self.assertEqual(code, 2)
        self.assertIn("/png:", err)

    def test_no_images_found(self):
        empty = self.base / "empty"
        empty.mkdir()
        code, _, err = support.run_cli(["/png:1", str(empty)], config=self.config)
        self.assertEqual(code, 1)
        self.assertIn("No images found", err)

    def test_missing_input_lands_in_the_not_found_bucket(self):
        code, out, err = support.run_cli(
            ["/png:1", "/outdir:false", "--tsv", str(self.images),
             str(self.base / "нет-такого")],
            tools_dir=self.tools, config=self.config)
        self.assertIn("нет-такого", err + out)

    def test_dry_run_writes_nothing(self):
        out_dir = self.base / "dry"
        code, out, err = support.run_cli(
            ["/png:1", "/outdir:%s" % out_dir, "--dry-run", str(self.images)],
            tools_dir=self.tools, config=self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("a.png", out)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_doctor_lists_tools_and_commands(self):
        code, out, err = support.run_cli(["--doctor"], tools_dir=self.tools,
                                         config=self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("optipng", out)
        self.assertIn("PNG Advanced", out)
        # Точный argv — это и есть документация, которая не может разъехаться.
        self.assertIn("-out", out)

    def test_version(self):
        code, out, _ = support.run_cli(["--version"], config=self.config)
        self.assertEqual(code, 0)
        self.assertIn("Image Catalyst", out)


class HostileEnvironmentTest(unittest.TestCase):
    """Прогон в отдельном процессе с враждебной локалью и перенаправлением."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = support.empty_config(self.base)
        self.images = self.base / "Тест — тире"
        self.images.mkdir()
        (self.images / "Ёлка «ель».png").write_bytes(
            corpus.png_bytes(width=32, height=32, text=b"q" * 200))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **env):
        environment = dict(os.environ)
        environment.update({
            "ICATALYST_TOOLS_DIR": str(self.tools),
            "ICATALYST_TOOLS_ONLY": "1",
            "ICATALYST_NO_TITLE": "1",
            "PYTHONPATH": str(REPO_ROOT),
        })
        environment.update(env)
        return subprocess.run(
            [sys.executable, "-m", "icatalyst", "--config", str(self.config),
             "/png:1", "/outdir:%s" % (self.base / "out"), str(self.images)],
            cwd=str(REPO_ROOT), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_ascii_io_encoding_does_not_crash(self):
        """Перенаправленный поток в Windows откатывается к кодовой странице локали.

        Именно на этом падал бы UnicodeEncodeError ровно на тех именах, которые
        мы починили, поэтому потоки принудительно переводятся в UTF-8 с
        backslashreplace.
        """
        proc = self._run(PYTHONIOENCODING="ascii")
        combined = proc.stdout.decode("utf-8", "replace") + \
            proc.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("UnicodeEncodeError", combined)
        self.assertEqual(proc.returncode, 0, combined)

    def test_posix_locale_does_not_crash(self):
        proc = self._run(LC_ALL="POSIX", LANG="POSIX")
        combined = proc.stdout.decode("utf-8", "replace") + \
            proc.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", combined)
        self.assertEqual(proc.returncode, 0, combined)

    def test_redirected_output_has_no_escape_sequences(self):
        proc = self._run()
        self.assertNotIn(b"\x1b", proc.stdout)
        self.assertNotIn(b"\x1b", proc.stderr)


def _all_files(root: Path):
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            yield Path(base) / name


if __name__ == "__main__":
    unittest.main()
