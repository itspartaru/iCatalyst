"""Паритет с версией 2.7: сравнение размеров, не требующее запуска 2.7 в CI.

Доказать нужно одно: **новое ядро сжимает не хуже 2.7**. Изначально это
проверялось живым запуском старого `.bat` на windows-раннере, и делать так не
следует. Цепочка 2010 года опирается на то, чего в современном окружении уже
нет или что ведёт себя иначе:

* `:getpid` вызывает `wmic`, удалённый из свежих образов Windows;
* `:dopause` ждёт нажатия клавиши, если видит свой путь в `%CMDCMDLINE%`;
* `:waithread` крутит цикл без выхода в ожидании файлов-блокировок, а рабочие
  процессы создаются через `start /b`, то есть внуками, до которых таймаут
  `subprocess` не достаёт;
* `sDOS2WIN` в `filter.js` создаёт объект `ADODB.Stream` на каждую строку ввода
  и ни разу его не закрывает.

Замерено на раннере: прогон встаёт сразу после «Loading. Please wait...», то
есть на вызове `cscript //E:JScript filter.js`. Держать релизы заложником
работоспособности этого кода в чужом окружении неправильно, и точнее сравнение
от такого запуска не становится.

Поэтому механизмов два.

`BaselineTest` выполняется всегда: он сравнивает новое ядро с **записанными**
размерами настоящего прогона 2.7 (`tests/parity_baseline.json`). Быстро,
детерминированно, без cmd.exe и WSH.

`LiveParityTest` сравнивает с живым запуском 2.7 и включается явно, через
`ICATALYST_PARITY=run`. Он предназначен для машины, где 2.7 действительно
работает — там сравнение и осмысленно. `ICATALYST_PARITY=record` тем же прогоном
записывает эталон, который затем коммитится и служит CI постоянно.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict

from icatalyst import imgcheck
from tests import corpus, support

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_BAT = REPO_ROOT / "iCatalyst.bat"
LEGACY_APPS = REPO_ROOT / "Tools" / "apps"
LEGACY_CONFIG = REPO_ROOT / "Tools" / "config.ini"
BASELINE = Path(__file__).resolve().parent / "parity_baseline.json"

_REQUIRED_APPS = ("truepng.exe", "deflopt.exe", "advdef.exe", "jpegtran.exe",
                  "gifsicle.exe", "pngwolfzopfli.exe")

#: Режимы, в которых снимается эталон: первый уровень для каждого формата.
MODES = ("/png:1", "/jpg:1", "/gif:1")


def legacy_available() -> bool:
    if os.name != "nt" or not LEGACY_BAT.is_file():
        return False
    return all((LEGACY_APPS / name).is_file() for name in _REQUIRED_APPS)


def parity_mode() -> str:
    return (os.environ.get("ICATALYST_PARITY") or "").strip().lower()


def build_fixtures(directory: Path) -> Dict[str, bytes]:
    """Корпус для сравнения: только ASCII-имена.

    Кириллицу версия 2.7 как раз и теряла, поэтому сравнивать на ней нечего —
    это и есть починенный баг. Здесь проверяется другое: что при равных условиях
    новое ядро сжимает не хуже.
    """
    directory.mkdir(parents=True, exist_ok=True)
    sources: Dict[str, bytes] = {}
    for index, (color_type, depth) in enumerate(
            [(0, 1), (0, 8), (2, 8), (3, 4), (4, 8), (6, 8)]):
        name = "png_%d_%d.png" % (color_type, depth)
        data = corpus.png_bytes(width=64, height=64, color_type=color_type,
                                bit_depth=depth, seed=index, text=b"m" * 300)
        (directory / name).write_bytes(data)
        sources[name] = data
    for progressive in (False, True):
        name = "jpeg_%s.jpg" % ("prog" if progressive else "base")
        data = corpus.jpeg_bytes(progressive=progressive, comment=b"m" * 500)
        (directory / name).write_bytes(data)
        sources[name] = data
    for frames in (1, 3):
        name = "gif_%d.gif" % frames
        data = corpus.gif_bytes(width=48, height=32, frames=frames, loop=0,
                                comment=b"m" * 400)
        (directory / name).write_bytes(data)
        sources[name] = data
    return sources


def run_new_core(images: Path, out_dir: Path, timeout: float = 420) -> Dict[str, int]:
    """Прогнать новое ядро и вернуть размеры результатов по именам файлов."""
    environment = dict(os.environ)
    environment.update({
        "ICATALYST_NO_TITLE": "1",
        "ICATALYST_PICKER": "terminal",
        "PYTHONPATH": str(REPO_ROOT),
    })
    proc = subprocess.run(
        [sys.executable, "-m", "icatalyst", *MODES, "/outdir:%s" % out_dir,
         "--tsv", "--no-pause", "--threads", "1", str(images)],
        cwd=str(REPO_ROOT), env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError("новое ядро завершилось с кодом %d:\n%s"
                             % (proc.returncode,
                                proc.stderr.decode("utf-8", "replace")))
    rows = support.tsv_rows(proc.stdout.decode("utf-8", "replace"))
    return {Path(row["source"]).name: int(row["optimized"]) for row in rows}


def size_table(sources: Dict[str, bytes], recorded: Dict[str, int],
               produced: Dict[str, int]) -> str:
    lines = []
    for name in sorted(sources):
        old = recorded.get(name)
        new = produced.get(name)
        delta = (new - old) if (old is not None and new is not None) else 0
        lines.append("  %-20s исходник %7d  2.7 %7s  новое %7s  %+d"
                     % (name, len(sources[name]),
                        old if old is not None else "-",
                        new if new is not None else "-", delta))
    return "\n".join(lines)


@unittest.skipUnless(BASELINE.is_file(),
                     "эталон 2.7 не записан: ICATALYST_PARITY=record на Windows")
@unittest.skipUnless(os.name == "nt",
                     "записанные размеры относятся к Windows-цепочке 2.7")
class BaselineTest(unittest.TestCase):
    """Сравнение с записанным эталоном 2.7. Выполняется в CI всегда."""

    @classmethod
    def setUpClass(cls):
        with open(BASELINE, "r", encoding="utf-8") as fh:
            cls.baseline = json.load(fh)
        cls.recorded = {k: int(v) for k, v in cls.baseline["sizes"].items()}
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name).resolve()
        cls.images = cls.base / "corpus"
        cls.sources = build_fixtures(cls.images)
        cls.out = cls.base / "new"
        cls.produced = run_new_core(cls.images, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_recorded_file_is_produced(self):
        missing = sorted(set(self.recorded) - set(self.produced))
        self.assertEqual(missing, [], "новое ядро не выдало: %r" % missing)

    def test_new_core_is_never_worse(self):
        worse = [(name, self.recorded[name], self.produced[name])
                 for name in sorted(self.recorded)
                 if name in self.produced
                 and self.produced[name] > self.recorded[name]]
        table = size_table(self.sources, self.recorded, self.produced)
        print("\nСравнение с эталоном 2.7 (%s):\n%s"
              % (self.baseline.get("recorded_on", "дата не указана"), table))
        self.assertEqual(worse, [], "новое ядро сжало хуже:\n%s" % table)

    def test_results_are_lossless(self):
        for name in sorted(self.sources):
            path = self.out / "corpus" / name
            if not path.is_file():
                continue
            fmt = {".png": "png", ".jpg": "jpg", ".gif": "gif"}[Path(name).suffix]
            with self.subTest(name=name):
                data = path.read_bytes()
                imgcheck.validate_data(data, fmt)
                if fmt == "jpg":
                    continue
                problem = imgcheck.pixels_equal(self.sources[name], data, fmt,
                                                allow_dirty_transparent=True)
                self.assertIsNone(problem, problem)

    def test_gif_keeps_looping(self):
        """В 2.7 `--no-extensions` делал циклящийся GIF одноразовым."""
        for name in sorted(n for n in self.sources if n.endswith(".gif")):
            path = self.out / "corpus" / name
            if not path.is_file():
                continue
            with self.subTest(name=name):
                info = imgcheck.read_gif(path.read_bytes(), with_frames=False)
                self.assertEqual(info.loop_count, 0)


@unittest.skipUnless(legacy_available() and parity_mode() in ("run", "record"),
                     "живое сравнение включается через ICATALYST_PARITY=run")
class LiveParityTest(unittest.TestCase):
    """Сравнение с живым запуском 2.7. Только по явному требованию.

    Предназначено для машины, где цепочка 2.7 работает. В свежих образах
    windows-раннера она встаёт на `cscript //E:JScript filter.js`, а `wmic`
    оттуда удалён.
    """

    TIMEOUT = 900

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name).resolve()
        cls.images = cls.base / "corpus"
        cls.sources = build_fixtures(cls.images)

        # Конфигурация 2.7 правится на время прогона и возвращается обратно.
        # `update=false` — иначе `:end` ждёт файл-флаг проверки обновлений.
        # `thread=1` — иначе `:waithread` может крутиться без выхода, ожидая
        # блокировки внуков, до которых таймаут не достаёт.
        cls._saved_config = LEGACY_CONFIG.read_bytes()
        text = cls._saved_config.decode("cp1251", "replace")
        text = text.replace("update=true", "update=false")
        text = text.replace("thread=0", "thread=1")
        LEGACY_CONFIG.write_bytes(text.encode("cp1251", "replace"))

        cls.old_dir = cls.base / "old"
        cls.old_log = cls._run_legacy(cls.old_dir)
        cls.recorded = cls._sizes(cls.old_dir / "corpus")
        cls.produced = run_new_core(cls.images, cls.base / "new", cls.TIMEOUT)

        if parity_mode() == "record":
            cls._write_baseline()

    @classmethod
    def tearDownClass(cls):
        LEGACY_CONFIG.write_bytes(cls._saved_config)
        cls._tmp.cleanup()

    @classmethod
    def _sizes(cls, directory: Path) -> Dict[str, int]:
        if not directory.is_dir():
            return {}
        return {p.name: p.stat().st_size for p in directory.glob("*") if p.is_file()}

    @classmethod
    def _write_baseline(cls) -> None:
        import datetime
        payload = {
            "comment": [
                "Размеры, полученные настоящей цепочкой версии 2.7.",
                "Записано командой ICATALYST_PARITY=record на Windows.",
                "CI сравнивает новое ядро с этими числами, не запуская 2.7:",
                "её код опирается на wmic, WSH и циклы ожидания без выхода.",
            ],
            "modes": list(MODES),
            "recorded_on": datetime.datetime.now().strftime("%Y-%m-%d"),
            "platform": sys.platform,
            "sizes": cls.recorded,
        }
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nэталон записан: %s" % BASELINE)

    @classmethod
    def _run_legacy(cls, out_dir: Path) -> str:
        """Запустить 2.7 через обёртку с `call`.

        Обёртка нужна из-за `:dopause` (`iCatalyst.bat:1268`): он ищет свой путь
        в `%CMDCMDLINE%` и, найдя, выполняет `pause`. С `call` из промежуточного
        файла там оказывается путь обёртки — ровно так, как README и предписывает
        вызывать программу.
        """
        wrapper = cls.base / "run_legacy.bat"
        wrapper.write_text(
            "@echo off\r\n"
            'call "%s" %s "/outdir:%s" "%s"\r\n'
            "exit /b %%errorlevel%%\r\n"
            % (LEGACY_BAT, " ".join(MODES), out_dir, cls.images),
            encoding="cp866", errors="replace")
        try:
            proc = subprocess.run(
                ["cmd.exe", "/c", str(wrapper)], cwd=str(REPO_ROOT),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=cls.TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            partial = (exc.output or b"").decode("cp866", "replace")
            raise AssertionError(
                "цепочка 2.7 не завершилась за %d с. Последний вывод:\n%s"
                % (cls.TIMEOUT, partial[-3000:]))
        return proc.stdout.decode("cp866", "replace")

    def test_both_implementations_produced_every_file(self):
        missing = sorted(set(self.sources) - set(self.recorded))
        self.assertEqual(missing, [],
                         "2.7 не выдала: %r\nлог:\n%s"
                         % (missing, self.old_log[-2000:]))

    def test_new_core_is_never_worse(self):
        worse = [(name, self.recorded[name], self.produced[name])
                 for name in sorted(self.recorded)
                 if name in self.produced
                 and self.produced[name] > self.recorded[name]]
        table = size_table(self.sources, self.recorded, self.produced)
        print("\nСравнение размеров:\n%s" % table)
        self.assertEqual(worse, [], "новое ядро сжало хуже:\n%s" % table)

    def test_jpeg_encoding_matches(self):
        for name in sorted(n for n in self.sources if n.endswith(".jpg")):
            old = self.old_dir / "corpus" / name
            new = self.base / "new" / "corpus" / name
            if not (old.is_file() and new.is_file()):
                continue
            with self.subTest(name=name):
                self.assertEqual(imgcheck.jpeg_encoding(new.read_bytes()),
                                 imgcheck.jpeg_encoding(old.read_bytes()))


if __name__ == "__main__":
    unittest.main()
