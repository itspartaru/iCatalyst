"""Проверки скрипта получения инструментов.

Особое внимание — проверке хешей: скрипт скачивает по сети **исполняемый код**,
и молча установить артефакт с неожидаемым содержимым он не имеет права.
Сетевые и компилирующие пути здесь не задействованы: они выполняются в CI.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import Tools.build_tools as bt  # noqa: N812
import os
from tests import support


def _load_build_deb():
    """Загрузить packaging/build_deb.py по пути, а не импортом пакета.

    В `packaging/` намеренно НЕТ `__init__.py`: с ним каталог становится обычным
    пакетом и затеняет одноимённую библиотеку с PyPI, от которой зависит сам
    PyInstaller (`import packaging.version` тогда падает). Без `__init__.py`
    работает правило PEP 420 и настоящий установленный пакет побеждает — но и
    импортировать наш модуль как `packaging.build_deb` уже нельзя.
    """
    import importlib.util
    from pathlib import Path as P

    path = P(__file__).resolve().parent.parent / "packaging" / "build_deb.py"
    spec = importlib.util.spec_from_file_location("icatalyst_build_deb", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LockFileTest(unittest.TestCase):
    def test_lock_file_is_valid_and_complete(self):
        lock = bt.load_lock()
        self.assertIn("oxipng", lock["downloads"])
        for name, entry in lock["downloads"].items():
            for platform, artifact in entry["artifacts"].items():
                with self.subTest(tool=name, platform=platform):
                    self.assertTrue(artifact["url"].startswith("https://"),
                                    "скачивание только по HTTPS")
                    self.assertEqual(len(artifact["sha256"]), 64)
                    self.assertTrue(artifact["member"])
                    self.assertTrue(artifact["install_as"])
        for name, entry in lock["builds"].items():
            with self.subTest(tool=name):
                self.assertTrue(entry["repo"].startswith("https://"))
                self.assertTrue(entry["tag"], "сборка обязана быть привязана к тегу")
                self.assertIn("license", entry)

    def test_pinned_versions_are_the_expected_ones(self):
        """Пины — часть контракта, а не деталь: смена версии меняет результат."""
        lock = bt.load_lock()
        self.assertEqual(lock["downloads"]["oxipng"]["version"], "10.1.1")
        # v1.1.2, а не вложенная в Windows 1.0.1: только в ней исправлена сборка
        # с современным zlib, и ценой этого поменялся синтаксис флагов.
        self.assertEqual(lock["builds"]["pngwolf"]["tag"], "v1.1.2")
        self.assertEqual(lock["builds"]["mozjpeg"]["tag"], "v4.1.1")

    def test_cmake_policy_escape_hatch_is_present(self):
        """На CMake >= 4.0 старый cmake_minimum_required — жёсткая ошибка."""
        lock = bt.load_lock()
        for name, entry in lock["builds"].items():
            with self.subTest(tool=name):
                self.assertIn("-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                              entry["cmake_args"])


class HashVerificationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dl = Path(self._tmp.name) / "dl"
        self._saved = bt.DL_DIR
        bt.DL_DIR = self.dl

    def tearDown(self):
        bt.DL_DIR = self._saved
        self._tmp.cleanup()

    def test_matching_hash_is_accepted(self):
        payload = b"correct payload"
        digest = hashlib.sha256(payload).hexdigest()
        with support.captured(), mock.patch.object(
                bt.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            path = bt._fetch("https://example.invalid/a.bin", digest)
        self.assertEqual(path.read_bytes(), payload)

    def test_mismatching_hash_is_rejected_and_nothing_is_saved(self):
        payload = b"tampered payload"
        wrong = hashlib.sha256(b"something else").hexdigest()
        with support.captured(), mock.patch.object(
                bt.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            with self.assertRaises(bt.BuildError) as caught:
                bt._fetch("https://example.invalid/a.bin", wrong)
        self.assertIn("хеш не совпал", str(caught.exception))
        # Ничего не должно остаться на диске: это исполняемый код из сети.
        self.assertEqual(list(self.dl.glob("*")) if self.dl.exists() else [], [])

    def test_cached_file_with_wrong_hash_is_redownloaded(self):
        payload = b"good"
        digest = hashlib.sha256(payload).hexdigest()
        self.dl.mkdir(parents=True)
        (self.dl / "a.bin").write_bytes(b"stale rubbish")
        with support.captured(), mock.patch.object(
                bt.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            path = bt._fetch("https://example.invalid/a.bin", digest)
        self.assertEqual(path.read_bytes(), payload)

    def test_network_failure_is_reported_clearly(self):
        with support.captured(), mock.patch.object(
                bt.urllib.request, "urlopen",
                side_effect=urllib.error.URLError("нет сети")):
            with self.assertRaises(bt.BuildError) as caught:
                bt._fetch("https://example.invalid/a.bin", "0" * 64)
        self.assertIn("не удалось скачать", str(caught.exception))


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _tar(self, member: str, payload: bytes) -> Path:
        path = self.base / "a.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return path

    def test_tar_member_is_extracted(self):
        archive = self._tar("oxipng-1.0-linux/oxipng", b"binary")
        result = bt._extract_member(archive, "oxipng-1.0-linux/oxipng",
                                    self.base / "into")
        self.assertEqual(result.read_bytes(), b"binary")

    def test_renamed_top_directory_still_resolves(self):
        """Каталог в архиве может называться иначе, чем записано в пине."""
        archive = self._tar("oxipng-9.9.9-other/oxipng", b"binary")
        result = bt._extract_member(archive, "oxipng-1.0-linux/oxipng",
                                    self.base / "into")
        self.assertEqual(result.read_bytes(), b"binary")

    def test_absent_member_is_an_error(self):
        archive = self._tar("something/else", b"x")
        with self.assertRaises(bt.BuildError):
            bt._extract_member(archive, "dir/oxipng", self.base / "into")

    def test_zip_member_is_extracted(self):
        path = self.base / "a.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("oxipng-1.0-win/oxipng.exe", b"pe32")
        result = bt._extract_member(path, "oxipng-1.0-win/oxipng.exe",
                                    self.base / "into")
        self.assertEqual(result.read_bytes(), b"pe32")


class StampTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._saved = bt.STAMP_DIR
        bt.STAMP_DIR = self.base / "stamp"

    def tearDown(self):
        bt.STAMP_DIR = self._saved
        self._tmp.cleanup()

    def test_step_is_skipped_only_when_binary_matches_the_stamp(self):
        target = self.base / "tool"
        target.write_bytes(b"v1")
        self.assertFalse(bt._is_current("t", "pin1", target))
        bt._write_stamp("t", "pin1", target)
        self.assertTrue(bt._is_current("t", "pin1", target))

        # Изменился пин — пересобрать.
        self.assertFalse(bt._is_current("t", "pin2", target))
        # Подменили бинарник — пересобрать.
        target.write_bytes(b"v2")
        self.assertFalse(bt._is_current("t", "pin1", target))
        # Файл удалили — пересобрать.
        target.unlink()
        self.assertFalse(bt._is_current("t", "pin1", target))

    def test_stamp_records_what_was_installed(self):
        target = self.base / "tool"
        target.write_bytes(b"payload")
        bt._write_stamp("t", "pin", target, {"version": "1.2.3"})
        saved = json.loads(next(bt.STAMP_DIR.glob("*.json")).read_text())
        self.assertEqual(saved["version"], "1.2.3")
        self.assertEqual(saved["sha256"], hashlib.sha256(b"payload").hexdigest())


class CommandLineTest(unittest.TestCase):
    def test_print_apt_lists_both_groups(self):
        with support.captured() as (out, _):
            code = bt.main(["--print-apt"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("optipng", text)
        self.assertIn("gifsicle", text)
        # Средства сборки — отдельной строкой: они нужны не всем.
        self.assertIn("cmake", text)
        self.assertIn("nasm", text)

    def test_check_reports_missing_essentials(self):
        with support.environment(ICATALYST_TOOLS_DIR=tempfile.mkdtemp(),
                                 ICATALYST_TOOLS_ONLY="1",
                                 ICATALYST_CONFIG=None):
            with support.captured() as (out, _):
                code = bt.main(["--check"])
        text = out.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn("НЕДОСТУПЕН", text)
        # Подсказка должна соответствовать системе: совет про apt на Windows
        # только сбивает с толку, там инструменты скачиваются нашим скриптом.
        if os.name == "nt":
            self.assertIn("build_tools.py --download", text)
            self.assertNotIn("sudo apt install", text)
        else:
            self.assertIn("sudo apt install", text)

    def test_check_asks_about_modes_not_about_tool_names(self):
        """На Windows нет optipng, но роль его играет TruePNG.

        Раньше список обязательных инструментов был зашит как
        optipng/gifsicle/jpegtran, и работа CI на windows-раннере падала при
        полностью работоспособном наборе.
        """
        from pathlib import Path as P
        tools = P(support.install_fake_tools(
            P(support.tempdir()) / "bin",
            only=["truepng", "deflopt", "advdef", "jpegtran", "gifsicle",
                  "pngwolf", "oxipng"]))
        config = support.posix_config(tools.parent)  # перезапишем ниже
        config.write_text("[options]\nprofile=windows\n", encoding="utf-8")
        with support.environment(ICATALYST_TOOLS_DIR=str(tools),
                                 ICATALYST_TOOLS_ONLY="1",
                                 ICATALYST_CONFIG=str(config)):
            with support.captured() as (out, _):
                code = bt.check()
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("Все форматы работают", text)
        # optipng отсутствует, и это не повод считать набор нерабочим.
        self.assertNotIn("НЕДОСТУПЕН", text)

    def test_check_fails_when_a_format_has_no_working_mode(self):
        from pathlib import Path as P
        empty = P(support.tempdir()) / "empty"
        empty.mkdir()
        with support.environment(ICATALYST_TOOLS_DIR=str(empty),
                                 ICATALYST_TOOLS_ONLY="1",
                                 ICATALYST_CONFIG=None):
            with support.captured() as (out, _):
                code = bt.check()
        text = out.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn("НЕДОСТУПЕН", text)

    def test_clean_never_touches_bin_or_apps(self):
        """`--clean` удаляет только промежуточные результаты сборки."""
        apps = bt.TOOLS_DIR / "apps"
        apps_before = sorted(p.name for p in apps.glob("*")) if apps.is_dir() else None
        bin_before = (sorted(p.name for p in bt.BIN_DIR.glob("*"))
                      if bt.BIN_DIR.is_dir() else None)
        bt.BUILD_DIR.mkdir(parents=True, exist_ok=True)
        (bt.BUILD_DIR / "мусор").write_text("x", encoding="utf-8")

        with support.captured():
            self.assertEqual(bt.main(["--clean"]), 0)

        self.assertFalse(bt.BUILD_DIR.exists(), "Tools/build должен быть удалён")
        if apps_before is not None:
            self.assertEqual(sorted(p.name for p in apps.glob("*")), apps_before,
                             "вложенные Tools/apps трогать нельзя")
        if bin_before is not None:
            self.assertEqual(sorted(p.name for p in bt.BIN_DIR.glob("*")), bin_before,
                             "установленные Tools/bin трогать нельзя")


if __name__ == "__main__":
    unittest.main()


class DebPackageTest(unittest.TestCase):
    """Проверки .deb, не требующие ни dpkg, ни прав root."""

    def test_version_uses_a_tilde_for_prereleases(self):
        """`3.0.0.dev0` в Debian больше, чем `3.0.0`; с тильдой — меньше."""
        build_deb = _load_build_deb()
        self.assertEqual(build_deb.deb_version("3.0.0.dev0"), "3.0.0~dev0")
        self.assertEqual(build_deb.deb_version("3.0.0rc1"), "3.0.0~rc1")
        self.assertEqual(build_deb.deb_version("3.0.0"), "3.0.0")
        self.assertEqual(build_deb.deb_version("3.1"), "3.1")

    def test_essential_optimizers_are_hard_dependencies(self):
        build_deb = _load_build_deb()
        depends = " ".join(build_deb.DEPENDS)
        for package in ("python3", "optipng", "gifsicle"):
            self.assertIn(package, depends)
        # jpegtran переехал между пакетами, поэтому альтернатива обязательна.
        self.assertIn("|", depends)
        # Улучшающие сжатие — не обязательные: без них режим деградирует, а не
        # ломается, и заставлять их ставить неправильно.
        self.assertIn("zopfli", " ".join(build_deb.RECOMMENDS))

    @unittest.skipIf(os.name == "nt",
                     "права POSIX на Windows не значат ничего: каталоги там 0777")
    def test_config_is_declared_a_conffile(self):
        """Иначе dpkg затирал бы правки пользователя при обновлении."""
        import tempfile as tf
        from pathlib import Path as P
        build_deb = _load_build_deb()
        with tf.TemporaryDirectory() as tmp:
            root = P(tmp) / "root"
            build_deb.build_tree(root, "3.0.0~dev0")
            conffiles = (root / "DEBIAN" / "conffiles").read_text()
            self.assertIn("/etc/icatalyst/config.ini", conffiles)
            self.assertTrue((root / "etc/icatalyst/config.ini").is_file())
            # Каталоги обязаны быть 0755: 0775 даёт запись группе.
            for base, dirs, _names in os.walk(root):
                for name in dirs:
                    mode = (P(base) / name).stat().st_mode & 0o777
                    self.assertEqual(mode, 0o755, "%s/%s" % (base, name))

    @unittest.skipIf(os.name == "nt",
                     "/etc — понятие Linux; .deb под Windows не устанавливается")
    def test_system_config_is_used_when_nothing_else_exists(self):
        """При установке из пакета каталога Tools рядом с модулем нет."""
        import tempfile as tf
        from pathlib import Path as P
        from unittest import mock as m
        from icatalyst import config as cfgmod
        with tf.TemporaryDirectory() as tmp:
            system = P(tmp) / "etc" / "icatalyst" / "config.ini"
            system.parent.mkdir(parents=True)
            system.write_text("[options]\noutdir=false\n", encoding="utf-8")
            with m.patch.object(cfgmod, "SYSTEM_CONFIG", system), \
                    support.environment(ICATALYST_CONFIG=None,
                                        XDG_CONFIG_HOME=str(P(tmp) / "empty")):
                cfg = cfgmod.load(app_dir=P(tmp) / "usr/lib/python3/dist-packages")
        self.assertEqual(cfg.source, system)
        self.assertEqual(cfg.outdir, "false")
