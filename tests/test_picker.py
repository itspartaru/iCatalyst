"""Проверки выбора каталога.

Главное здесь — семантика отмены. В 2.7 Cancel в `browsefolder.exe` означал
«перезаписать оригиналы» (`iCatalyst.bat:163`), и это единственное, что стоит
между пользователем и заменой его файлов. Отдельно проверяется, что
**невозможность спросить** не приравнивается к отмене: молча уничтожать
оригиналы в пакетном запуске нельзя.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from icatalyst import picker
from tests import corpus, support

#: Абсолютный путь к интерпретатору: PATH в этих тестах заменяется целиком,
#: поэтому `/usr/bin/env` был бы недоступен.
_FAKE = """#!{python}
import sys
{body}
"""


def _write_tool(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(_FAKE.format(python=sys.executable, body=body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


class BackendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _with_path(self, **env):
        # PATH ЗАМЕНЯЕТСЯ целиком, а не дополняется: иначе shutil.which находит
        # настоящий zenity, установленный в системе, и тест открывает живой
        # диалог, который ждёт человека и вешает прогон.
        values = {"PATH": str(self.bin), "ICATALYST_PICKER": None,
                  "DISPLAY": ":0", "WAYLAND_DISPLAY": None,
                  "XDG_CURRENT_DESKTOP": "GNOME"}
        values.update(env)
        return support.environment(**values)

    def test_zenity_returns_the_chosen_directory(self):
        _write_tool(self.bin, "zenity", 'print("/tmp/выбранный каталог")')
        with self._with_path():
            result = picker.pick_directory("zenity")
        self.assertIs(result.choice, picker.Choice.DIR)
        self.assertEqual(result.path, Path("/tmp/выбранный каталог"))

    def test_zenity_cancel_means_overwrite_originals(self):
        """Код возврата 1 — штатная отмена, а не сбой."""
        _write_tool(self.bin, "zenity", "sys.exit(1)")
        with self._with_path():
            result = picker.pick_directory("zenity")
        self.assertIs(result.choice, picker.Choice.IN_PLACE)
        self.assertEqual(result.note, "")

    def test_zenity_crash_is_not_treated_as_cancel(self):
        """Сбой диалога обязан быть отмечен, а не выглядеть как отмена."""
        _write_tool(self.bin, "zenity", 'sys.stderr.write("boom\\n"); sys.exit(5)')
        with self._with_path():
            result = picker.pick_directory("zenity")
        self.assertIs(result.choice, picker.Choice.IN_PLACE)
        self.assertIn("5", result.note)

    def test_kdialog_returns_the_chosen_directory(self):
        _write_tool(self.bin, "kdialog", 'print("/tmp/kde")')
        with self._with_path():
            result = picker.pick_directory("kdialog")
        self.assertIs(result.choice, picker.Choice.DIR)
        self.assertEqual(result.path, Path("/tmp/kde"))

    def test_kdialog_cancel(self):
        _write_tool(self.bin, "kdialog", "sys.exit(1)")
        with self._with_path():
            result = picker.pick_directory("kdialog")
        self.assertIs(result.choice, picker.Choice.IN_PLACE)

    def test_missing_backend_is_reported_not_guessed(self):
        with self._with_path():
            result = picker.pick_directory("zenity")
        self.assertIs(result.choice, picker.Choice.IN_PLACE)
        self.assertIn("zenity", result.note)

    def test_unknown_backend_name(self):
        with self._with_path():
            result = picker.pick_directory("несуществующий")
        self.assertIn("несуществующий", result.note)

    def test_none_disables_the_dialog(self):
        with self._with_path():
            result = picker.pick_directory("none")
        self.assertIs(result.choice, picker.Choice.IN_PLACE)
        self.assertIn("picker=none", result.note)

    def test_environment_variable_overrides_the_argument(self):
        _write_tool(self.bin, "zenity", 'print("/tmp/из переменной")')
        with self._with_path(ICATALYST_PICKER="zenity"):
            result = picker.pick_directory("kdialog")
        self.assertEqual(result.path, Path("/tmp/из переменной"))

    def test_broken_backend_falls_through_to_the_next_one(self):
        """Сломанный zenity не должен решать за пользователя."""
        _write_tool(self.bin, "zenity", "sys.exit(5)")
        _write_tool(self.bin, "kdialog", 'print("/tmp/подхватил kdialog")')
        with self._with_path():
            chain = picker.backend_chain()
            self.assertIn("zenity", chain)
            self.assertIn("kdialog", chain)
            self.assertLess(chain.index("zenity"), chain.index("kdialog"))
            result = picker.pick_directory("auto")
        self.assertIs(result.choice, picker.Choice.DIR)
        self.assertEqual(result.path, Path("/tmp/подхватил kdialog"))
        self.assertIn("5", result.note)


class ChainTest(unittest.TestCase):
    def test_no_display_goes_straight_to_the_terminal(self):
        """Без дисплея zenity не запускается вовсе: он думает секунды и падает."""
        if os.name == "nt" or sys.platform == "darwin":
            self.skipTest("правило относится к X11 и Wayland")
        with support.environment(DISPLAY=None, WAYLAND_DISPLAY=None):
            self.assertEqual(picker.backend_chain(), ["terminal"])

    def test_kde_prefers_kdialog(self):
        if os.name == "nt":
            self.skipTest("не относится к Windows")
        tmp = tempfile.mkdtemp()
        _write_tool(Path(tmp), "kdialog", "pass")
        _write_tool(Path(tmp), "zenity", "pass")
        with support.environment(
                PATH="%s%s%s" % (tmp, os.pathsep, os.environ.get("PATH", "")),
                DISPLAY=":0", WAYLAND_DISPLAY=None,
                XDG_CURRENT_DESKTOP="KDE"):
            chain = picker.backend_chain()
        self.assertEqual(chain[0], "kdialog")

    def test_terminal_is_always_last(self):
        with support.environment(DISPLAY=None, WAYLAND_DISPLAY=None):
            self.assertEqual(picker.backend_chain()[-1], "terminal")


class CliIntegrationTest(unittest.TestCase):
    """Как результат выбора доезжает до записи файлов."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = support.empty_config(self.base)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.images = self.base / "Фото — копия"
        self.images.mkdir()
        self.png = self.images / "Ёлка «ель».png"
        self.png.write_bytes(corpus.png_bytes(width=32, height=32, text=b"m" * 300))
        self.original = self.png.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra, **env):
        values = {"PATH": str(self.bin), "DISPLAY": ":0", "WAYLAND_DISPLAY": None}
        values.update(env)
        return support.run_cli(["/png:1", "/jpg:0", "/gif:0", "--tsv",
                                *extra, str(self.images)],
                               tools_dir=self.tools, config=self.config, **values)

    def test_chosen_directory_is_used(self):
        target = self.base / "куда сохранить"
        _write_tool(self.bin, "zenity", 'print(%r)' % str(target))
        code, out, err = self._run("--picker", "zenity", ICATALYST_PICKER=None)
        self.assertEqual(code, 0, err)
        row = support.tsv_rows(out)[0]
        self.assertTrue(str(row["destination"]).startswith(str(target)))
        self.assertEqual(self.png.read_bytes(), self.original)

    def test_cancel_overwrites_the_originals(self):
        """Ровно то, что делал Cancel в browsefolder.exe."""
        _write_tool(self.bin, "zenity", "sys.exit(1)")
        code, out, err = self._run("--picker", "zenity", ICATALYST_PICKER=None)
        self.assertEqual(code, 0, err)
        row = support.tsv_rows(out)[0]
        self.assertEqual(Path(row["destination"]), self.png)
        self.assertEqual(row["status"], "ok")
        self.assertLess(self.png.stat().st_size, len(self.original))

    def test_batch_run_without_outdir_refuses_to_overwrite(self):
        """Спросить негде — значит остановиться, а не уничтожить оригиналы."""
        code, out, err = self._run(ICATALYST_PICKER=None)
        self.assertEqual(code, 2)
        self.assertIn("/outdir", err)
        self.assertEqual(self.png.read_bytes(), self.original)

    def test_explicit_outdir_skips_the_dialog_entirely(self):
        _write_tool(self.bin, "zenity", 'sys.exit("диалог не должен вызываться")')
        code, out, err = self._run("/outdir:%s" % (self.base / "явный"),
                                   ICATALYST_PICKER=None)
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
