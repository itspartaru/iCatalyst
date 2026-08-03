"""Харнесс паритета: старый `iCatalyst.bat` против нового ядра.

Это тот самый шлюз, по зелёному результату которого `.bat` превращается в
обёртку, а `Tools/scripts/*.js` удаляются. Пропускается везде, кроме Windows:
цепочка 2.7 опирается на cmd.exe, `cscript //E:JScript` и вложенные Win32-бинарники.

Корпус здесь **только из ASCII-имён**: старый код кириллицу и пунктуацию как
раз и терял, поэтому сравнивать на них нечего — это и есть починенный баг.
Здесь проверяется другое: что при равных условиях новое ядро сжимает не хуже и
так же без потерь.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from icatalyst import imgcheck
from tests import corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_BAT = REPO_ROOT / "iCatalyst.bat"
LEGACY_APPS = REPO_ROOT / "Tools" / "apps"
LEGACY_CONFIG = REPO_ROOT / "Tools" / "config.ini"

_REQUIRED_APPS = ("truepng.exe", "deflopt.exe", "advdef.exe", "jpegtran.exe",
                  "gifsicle.exe", "pngwolfzopfli.exe")


def _legacy_available() -> bool:
    if os.name != "nt":
        return False
    if not LEGACY_BAT.is_file():
        return False
    return all((LEGACY_APPS / name).is_file() for name in _REQUIRED_APPS)


@unittest.skipUnless(_legacy_available(),
                     "цепочка 2.7 требует Windows и вложенных Tools/apps/*.exe")
class ParityTest(unittest.TestCase):
    """Сравнение размеров и качества между 2.7 и новым ядром."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name).resolve()
        cls.images = cls.base / "corpus"
        cls.images.mkdir()
        cls.sources = {}
        for index, (color_type, depth) in enumerate(
                [(0, 1), (0, 8), (2, 8), (3, 4), (4, 8), (6, 8)]):
            name = "png_%d_%d.png" % (color_type, depth)
            data = corpus.png_bytes(width=64, height=64, color_type=color_type,
                                    bit_depth=depth, seed=index, text=b"m" * 300)
            (cls.images / name).write_bytes(data)
            cls.sources[name] = data
        for index, progressive in enumerate((False, True)):
            name = "jpeg_%s.jpg" % ("prog" if progressive else "base")
            data = corpus.jpeg_bytes(progressive=progressive, comment=b"m" * 500)
            (cls.images / name).write_bytes(data)
            cls.sources[name] = data
        for index, frames in enumerate((1, 3)):
            name = "gif_%d.gif" % frames
            data = corpus.gif_bytes(width=48, height=32, frames=frames, loop=0,
                                    comment=b"m" * 400)
            (cls.images / name).write_bytes(data)
            cls.sources[name] = data

        # Конфигурация 2.7 правится на время прогона и возвращается в tearDown.
        # Два изменения, и оба обязательны, иначе харнесс зависает:
        #
        # `update=false` — проверка обновлений стучится на x128.ho.ua по
        # открытому HTTP, а `:end` ждёт её файл-флаг циклом без выхода.
        #
        # `thread=1` — в многопоточном режиме `:createthread` порождает рабочие
        # процессы через `start /b`, а `:waithread` крутит `:waitflag` до
        # исчезновения файлов-блокировок. Если хоть один внук не смог удалить
        # свой .lck, цикл бесконечен, и убить его нельзя: subprocess прибивает
        # только прямого потомка, а не внуков. В однопоточном режиме
        # `:createthread` вызывает `:threadwork` напрямую, ни блокировок, ни
        # внуков не возникает, а для сравнения размеров потоки и не нужны.
        cls._saved_config = LEGACY_CONFIG.read_bytes()
        text = cls._saved_config.decode("cp1251", "replace")
        text = text.replace("update=true", "update=false")
        text = text.replace("thread=0", "thread=1")
        LEGACY_CONFIG.write_bytes(text.encode("cp1251", "replace"))

        cls.old_dir = cls.base / "old"
        cls.new_dir = cls.base / "new"
        cls.old_log = cls._run_legacy(cls.old_dir)
        cls.new_log = cls._run_new(cls.new_dir)

    @classmethod
    def tearDownClass(cls):
        LEGACY_CONFIG.write_bytes(cls._saved_config)
        cls._tmp.cleanup()

    #: Верхняя граница на прогон каждой реализации. Полчаса, стоявшие здесь
    #: раньше, означали, что подвисший харнесс просто занимает раннер и никакой
    #: диагностики не даёт.
    TIMEOUT = 420

    @classmethod
    def _run_legacy(cls, out_dir: Path) -> str:
        """Запустить цепочку 2.7 через обёртку с `call`.

        Обёртка обязательна. `:dopause` (`iCatalyst.bat:1268`) ищет собственный
        путь в `%CMDCMDLINE%` и, найдя, выполняет `pause` — так он отличает
        запуск двойным щелчком от вызова из скрипта. При `cmd /c <путь к .bat>`
        путь в командной строке присутствует, и прогон останавливается в
        ожидании нажатия клавиши. С `call` из промежуточного файла в
        `%CMDCMDLINE%` оказывается путь обёртки, и ветка паузы не срабатывает —
        ровно так, как README и предписывает вызывать программу.
        """
        wrapper = cls.base / "run_legacy.bat"
        wrapper.write_text(
            "@echo off\r\n"
            'call "%s" /png:1 /jpg:1 /gif:1 "/outdir:%s" "%s"\r\n'
            "exit /b %%errorlevel%%\r\n" % (LEGACY_BAT, out_dir, cls.images),
            encoding="cp866", errors="replace")
        return cls._capture(["cmd.exe", "/c", str(wrapper)], "cp866")

    @classmethod
    def _capture(cls, argv, encoding: str, env=None) -> str:
        try:
            proc = subprocess.run(
                argv, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=cls.TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            partial = (exc.output or b"").decode(encoding, "replace")
            raise AssertionError(
                "%s не завершилась за %d с. Последний вывод:\n%s"
                % (argv[0], cls.TIMEOUT, partial[-3000:]))
        return proc.stdout.decode(encoding, "replace")

    @classmethod
    def _run_new(cls, out_dir: Path) -> str:
        environment = dict(os.environ)
        environment.update({"ICATALYST_NO_TITLE": "1", "PYTHONPATH": str(REPO_ROOT),
                            "ICATALYST_PICKER": "terminal"})
        return cls._capture(
            [sys.executable, "-m", "icatalyst", "/png:1", "/jpg:1", "/gif:1",
             "/outdir:%s" % out_dir, "--tsv", "--no-pause", "--threads", "1",
             str(cls.images)],
            "utf-8", env=environment)

    def _pairs(self):
        """(имя, старый результат, новый результат) для каждого файла корпуса."""
        for name in sorted(self.sources):
            old = self.old_dir / "corpus" / name
            new = self.new_dir / "corpus" / name
            yield name, old, new

    def test_both_implementations_produced_every_file(self):
        missing = [(name, old.exists(), new.exists())
                   for name, old, new in self._pairs()
                   if not (old.exists() and new.exists())]
        self.assertEqual(missing, [], "старый лог:\n%s" % self.old_log[-2000:])

    def test_new_core_is_never_worse(self):
        rows = []
        worse = []
        for name, old, new in self._pairs():
            if not (old.exists() and new.exists()):
                continue
            source = len(self.sources[name])
            old_size, new_size = old.stat().st_size, new.stat().st_size
            rows.append((name, source, old_size, new_size))
            if new_size > old_size:
                worse.append((name, old_size, new_size))
        table = "\n".join(
            "  %-20s исходник %7d  2.7 %7d  новое %7d  %+d"
            % (name, source, old_size, new_size, new_size - old_size)
            for name, source, old_size, new_size in rows)
        # Таблица печатается всегда: числа нужны в README, а не оценка на глаз.
        print("\nСравнение размеров:\n%s" % table)
        self.assertEqual(worse, [], "новое ядро сжало хуже:\n%s" % table)

    def test_both_results_are_lossless_against_the_source(self):
        for name, old, new in self._pairs():
            if not new.exists():
                continue
            fmt = {".png": "png", ".jpg": "jpg", ".gif": "gif"}[Path(name).suffix]
            with self.subTest(name=name):
                imgcheck.validate_data(new.read_bytes(), fmt)
                if fmt == "jpg":
                    continue
                problem = imgcheck.pixels_equal(
                    self.sources[name], new.read_bytes(), fmt,
                    allow_dirty_transparent=True)
                self.assertIsNone(problem, "новое ядро: %s" % problem)
                if old.exists():
                    problem = imgcheck.pixels_equal(
                        self.sources[name], old.read_bytes(), fmt,
                        allow_dirty_transparent=True)
                    self.assertIsNone(problem, "2.7: %s" % problem)

    def test_jpeg_encoding_matches_between_implementations(self):
        for name, old, new in self._pairs():
            if not name.endswith(".jpg") or not (old.exists() and new.exists()):
                continue
            with self.subTest(name=name):
                self.assertEqual(imgcheck.jpeg_encoding(new.read_bytes()),
                                 imgcheck.jpeg_encoding(old.read_bytes()))

    def test_gif_loop_count_is_preserved_by_the_new_core(self):
        """В 2.7 `--no-extensions` делал циклящийся GIF одноразовым."""
        for name, old, new in self._pairs():
            if not name.endswith(".gif") or not new.exists():
                continue
            with self.subTest(name=name):
                info = imgcheck.read_gif(new.read_bytes(), with_frames=False)
                self.assertEqual(info.loop_count, 0)


if __name__ == "__main__":
    unittest.main()
