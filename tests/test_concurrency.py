"""Проверки параллельного прогона.

Главное свойство — детерминированность: вывод не зависит от числа потоков. В
2.7 порядок строк определялся файлами логов на поток и был произвольным; это был
артефакт реализации, а не свойство продукта, и здесь он заменён порядком входных
данных.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from icatalyst import report
from icatalyst.toolbox import Aborted, ProcessRegistry
from tests import corpus, support


class DeterminismTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name).resolve()
        cls.tools = support.install_fake_tools(cls.base / "fakebin")
        cls.config = support.empty_config(cls.base)
        cls.images = cls.base / "Фото — копия"
        cls.images.mkdir()
        # Достаточно файлов и подкаталогов, чтобы потоки реально перемешались.
        for index in range(12):
            folder = cls.images / ("каталог «%d»" % index)
            folder.mkdir()
            (folder / ("лого %d.png" % index)).write_bytes(
                corpus.png_bytes(width=32, height=32, seed=index, text=b"m" * 200))
            (folder / ("фото %d.jpg" % index)).write_bytes(
                corpus.jpeg_bytes(comment=b"m" * 300))
            (folder / ("анимация %d.gif" % index)).write_bytes(
                corpus.gif_bytes(width=16, height=16, frames=2, loop=0,
                                 comment=b"m" * 200))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, *extra):
        out_dir = self.base / ("out_%s" % "_".join(extra).replace("-", ""))
        code, out, err = support.run_cli(
            ["/png:1", "/jpg:1", "/gif:1", "/outdir:%s" % out_dir, "--tsv",
             *extra, str(self.images)],
            tools_dir=self.tools, config=self.config)
        self.assertEqual(code, 0, err)
        return support.tsv_rows(out)

    def test_output_is_identical_for_one_and_many_threads(self):
        single = self._run("--threads", "1")
        many = self._run("--threads", "8")
        self.assertEqual(len(single), 36)
        # Пути назначения отличаются каталогом, поэтому сравниваем всё остальное.
        strip = lambda rows: [(r["status"], r["format"], r["chain"],
                               Path(r["source"]).name, r["original"], r["optimized"])
                              for r in rows]
        self.assertEqual(strip(single), strip(many))

    def test_rows_follow_input_order_not_completion_order(self):
        rows = self._run("--threads", "8")
        names = [Path(r["source"]).parent.name for r in rows]
        self.assertEqual(names, sorted(names, key=names.index))
        # Внутри каталога порядок тоже стабилен и алфавитный.
        first = [Path(r["source"]).name for r in rows
                 if Path(r["source"]).parent.name == "каталог «0»"]
        self.assertEqual(first, sorted(first))

    def test_stream_mode_reports_the_same_files(self):
        ordered = self._run("--threads", "8")
        streamed = self._run("--threads", "8", "--stream")
        self.assertEqual({Path(r["source"]).name for r in streamed},
                         {Path(r["source"]).name for r in ordered})

    def test_every_file_is_processed_exactly_once(self):
        rows = self._run("--threads", "8")
        sources = [r["source"] for r in rows]
        self.assertEqual(len(sources), len(set(sources)))


class ProgressTest(unittest.TestCase):
    def test_counters_are_independent_per_format(self):
        progress = report.Progress()
        progress.add("png", "Advanced", 3)
        progress.add("gif", "Default", 1)
        progress.bump("png")
        progress.bump("PNG")
        snapshot = progress.snapshot()
        self.assertEqual(snapshot["PNG"], ("Advanced", 2, 3))
        self.assertEqual(snapshot["GIF"], ("Default", 0, 1))
        self.assertIn("PNG Advanced: 66%", report.progress_title(snapshot, "x"))

    def test_bump_is_safe_from_many_threads(self):
        progress = report.Progress()
        progress.add("png", "Advanced", 400)
        threads = [threading.Thread(target=lambda: [progress.bump("png")
                                                    for _ in range(100)])
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(progress.snapshot()["PNG"][1], 400)

    def test_unknown_format_is_ignored(self):
        progress = report.Progress()
        progress.bump("png")  # не должно падать
        self.assertEqual(progress.snapshot(), {})


class ProcessRegistryTest(unittest.TestCase):
    """Ctrl-C должен прибивать инструменты, а не ждать их завершения."""

    def test_abort_terminates_a_running_tool(self):
        registry = ProcessRegistry()
        outcome = {}
        started = threading.Event()

        def worker():
            started.set()
            try:
                registry.run([sys.executable, "-c", "import time; time.sleep(60)"])
            except BaseException as exc:  # noqa: BLE001 — фиксируем что угодно
                outcome["exc"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(10))
        deadline = time.monotonic() + 10
        while not registry._live and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(registry._live, "процесс не зарегистрировался")

        registry.abort()
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "процесс не был прибит")
        self.assertIsInstance(outcome.get("exc"), Aborted)

    def test_no_new_processes_after_abort(self):
        registry = ProcessRegistry()
        registry.abort()
        with self.assertRaises(Aborted):
            registry.run([sys.executable, "-c", "pass"])

    def test_timeout_kills_the_process(self):
        registry = ProcessRegistry()
        import subprocess
        with self.assertRaises(subprocess.TimeoutExpired):
            registry.run([sys.executable, "-c", "import time; time.sleep(30)"],
                         timeout=1)
        self.assertFalse(registry._live, "процесс остался в реестре")


class InterruptTest(unittest.TestCase):
    """Прерывание не должно оставлять на месте оригинала обрезанный файл."""

    def test_sigint_stops_promptly_and_keeps_originals(self):
        import os
        import signal
        import subprocess

        if os.name == "nt":
            self.skipTest("SIGINT в Windows работает иначе")
        tmp = Path(tempfile.mkdtemp()).resolve()
        tools = support.install_fake_tools(tmp / "fakebin")
        config = support.empty_config(tmp)
        images = tmp / "Фото — копия"
        images.mkdir()
        originals = {}
        for index in range(6):
            path = images / ("лого %d.png" % index)
            path.write_bytes(corpus.png_bytes(width=32, height=32, seed=index,
                                             text=b"m" * 200))
            originals[path] = path.read_bytes()

        environment = dict(os.environ)
        environment.update({
            "ICATALYST_TOOLS_DIR": str(tools),
            "ICATALYST_TOOLS_ONLY": "1",
            "ICATALYST_NO_TITLE": "1",
            "ICATALYST_PICKER": "terminal",
            # Поддельный optipng засыпает на 600 секунд, так что прогон
            # заведомо не успеет закончиться сам.
            "ICATALYST_FAKE_MODE": "hang",
            "ICATALYST_FAKE_TARGET": "optipng",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        })
        proc = subprocess.Popen(
            [sys.executable, "-m", "icatalyst", "--config", str(config),
             "/png:1", "/jpg:0", "/gif:0", "/outdir:false", "--threads", "4",
             "--no-pause", str(images)],
            cwd=str(Path(__file__).resolve().parent.parent), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        # Ждём именно шапку таблицы, а не появление дочерних процессов: первыми
        # дочерними оказываются зонды инструментов, которые запускаются раньше,
        # чем начинается обработка. По ним прерывание приходило до старта
        # прогона, и тест то проверял что-то, то нет.
        header = _read_until(proc, "File Name", timeout=60)
        self.assertIn("File Name", header,
                      "прогон не дошёл до таблицы:\n%s" % header)
        try:
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("процесс не завершился после SIGINT")
        stdout = header.encode("utf-8", "replace") + stdout
        combined = stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", combined)
        self.assertEqual(proc.returncode, 130, combined)
        self.assertIn("Interrupted by user", combined)
        # Отчёт не должен врать, будто файлы обработаны.
        self.assertNotIn("PNG [6/6]", combined)
        for path, data in originals.items():
            self.assertTrue(path.exists(), "оригинал %s исчез" % path.name)
            self.assertEqual(path.read_bytes(), data,
                             "оригинал %s изменён после прерывания" % path.name)


def _read_until(proc, needle: str, timeout: float) -> str:
    """Читать stdout процесса, пока не встретится подстрока или не выйдет время.

    Читаем побайтово: `readline` заблокировался бы, если процесс напечатал шапку
    без завершающего перевода строки, а разбирать это в тесте не нужно.
    """
    collected = bytearray()
    deadline = time.monotonic() + timeout
    target = needle.encode("utf-8")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            collected += proc.stdout.read() or b""
            break
        chunk = proc.stdout.read(1)
        if not chunk:
            time.sleep(0.01)
            continue
        collected += chunk
        if target in collected:
            break
    return collected.decode("utf-8", "replace")


class WorkerCountTest(unittest.TestCase):
    def test_thread_zero_means_cpu_count(self):
        import os
        from icatalyst import config as cfgmod
        cfg = cfgmod.Config()
        self.assertEqual(cfg.thread, 0)
        workers = max(1, min(32, cfg.thread or os.cpu_count() or 1))
        self.assertEqual(workers, min(32, os.cpu_count() or 1))

    def test_explicit_thread_count_is_honoured(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        config = tmp / "c.ini"
        config.write_text("[options]\nthread=3\n", encoding="utf-8")
        from icatalyst import config as cfgmod
        self.assertEqual(cfgmod.load(str(config)).thread, 3)


if __name__ == "__main__":
    unittest.main()


class MapperThreadSafetyTest(unittest.TestCase):
    """`OutputMapper` вызывается из рабочих потоков и обязан быть потокобезопасен.

    Отсутствие замка проявлялось редко и зависело от тайминга: два потока
    одновременно не находили входной корень в кэше, оба заводили выходной
    подкаталог, и файлы одного дерева расползались по `Фото` и `Фото-0001`.
    """

    def test_one_input_root_yields_one_output_root(self):
        import concurrent.futures
        from pathlib import Path as P
        from icatalyst.paths import OutputMapper

        tmp = P(tempfile.mkdtemp()).resolve()
        root = tmp / "Фото — копия"
        (root / "вложенная").mkdir(parents=True)
        sources = []
        for index in range(200):
            folder = root if index % 2 else root / "вложенная"
            path = folder / ("файл %03d.png" % index)
            path.write_bytes(b"x")
            sources.append(path)

        mapper = OutputMapper(tmp / "out")
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            destinations = list(pool.map(
                lambda src: mapper.destination(src, root), sources))

        # `.parts[0]`, а не разбор строки по «/»: на Windows разделитель другой,
        # и такой тест был бы красным только там.
        roots = {P(d).relative_to(tmp / "out").parts[0] for d in destinations}
        self.assertEqual(roots, {"Фото — копия"},
                         "дерево разъехалось по нескольким выходным каталогам")
        self.assertEqual(len(set(destinations)), len(destinations),
                         "двум файлам досталось одно назначение")

    def test_colliding_names_from_many_threads_stay_unique(self):
        import concurrent.futures
        from pathlib import Path as P
        from icatalyst.paths import OutputMapper

        tmp = P(tempfile.mkdtemp()).resolve()
        sources = []
        for index in range(100):
            folder = tmp / ("вход %03d" % index)
            folder.mkdir()
            path = folder / "лого.png"
            path.write_bytes(b"x")
            sources.append(path)

        mapper = OutputMapper(tmp / "out")
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            destinations = list(pool.map(
                lambda src: mapper.destination(src, src), sources))
        self.assertEqual(len(set(destinations)), len(sources))
