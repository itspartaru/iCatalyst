"""Проверки Windows-цепочки — на любой машине, а не только на windows-раннере.

Закрытые TruePNG, DeflOpt и pngwolf-zopfli подменяются подделками, поэтому здесь
проверяется именно наша логика: скрейп параметров zlib из лога TruePNG, выбор
формы командной строки pngwolf и структура цепочки. Настоящие бинарники
исполняются в CI (`Tools/apps/*.exe` лежат в git ровно для этого).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from icatalyst import config as cfgmod
from icatalyst import imgcheck, recipes
from tests import corpus, support


def _windows_config(directory: Path, extra: str = "") -> Path:
    path = Path(directory) / "windows.ini"
    path.write_text("[options]\nprofile=windows\n" + extra, encoding="utf-8")
    return path


class ProfileSelectionTest(unittest.TestCase):
    def test_profile_key_forces_the_toolset(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        cfg = cfgmod.load(str(_windows_config(tmp)))
        self.assertEqual(cfg.profile, "windows")
        self.assertTrue(recipes.use_windows_profile(cfg))
        recipe = recipes.build("png", 1, cfg)
        # TruePNG-цепочка первая и дословная, oxipng — второй кандидат в гонке.
        self.assertEqual([c.name for c in recipe.chains], ["truepng", "oxipng"])

    def test_posix_profile_can_be_forced_too(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        path = tmp / "c.ini"
        path.write_text("[options]\nprofile=posix\n", encoding="utf-8")
        cfg = cfgmod.load(str(path))
        self.assertFalse(recipes.use_windows_profile(cfg))
        recipe = recipes.build("png", 1, cfg)
        self.assertIn("optipng", recipe.chains[0].name)

    def test_explicit_profile_disables_the_fallback(self):
        """Пользователь выбрал набор сознательно — молча подменять его нельзя."""
        tmp = Path(tempfile.mkdtemp()).resolve()
        cfg = cfgmod.load(str(_windows_config(tmp)))
        self.assertIsNone(recipes.fallback_recipe("png", 1, cfg))
        auto = cfgmod.Config()
        self.assertIsNotNone(recipes.fallback_recipe("png", 1, auto))

    def test_invalid_profile_is_rejected(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        path = tmp / "c.ini"
        path.write_text("[options]\nprofile=bsd\n", encoding="utf-8")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load(str(path))


class WindowsChainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = _windows_config(self.base)
        self.images = self.base / "Фото — копия"
        self.images.mkdir()
        self.png = self.images / "Ёлка «ель».png"
        self.png.write_bytes(corpus.png_bytes(width=48, height=48, text=b"m" * 300))
        self.original = self.png.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, mode: int, *extra, **env):
        out = self.base / ("out%d%s" % (mode, "".join(extra).replace("-", "")))
        code, stdout, err = support.run_cli(
            ["/png:%d" % mode, "/jpg:0", "/gif:0", "/outdir:%s" % out, "--tsv",
             *extra, str(self.images)],
            tools_dir=self.tools, config=self.config, **env)
        rows = support.tsv_rows(stdout)
        return code, (rows[0] if rows else None), err

    def test_advanced_chain_runs_and_is_lossless(self):
        code, row, err = self._run(1)
        self.assertEqual(code, 0, err)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["chain"], "truepng")
        self.assertLess(int(row["optimized"]), int(row["original"]))
        self.assertIsNone(imgcheck.pixels_equal(
            self.original, Path(row["destination"]).read_bytes(), "png"))

    def test_xtreme_chain_runs_and_is_lossless(self):
        code, row, err = self._run(2)
        self.assertEqual(code, 0, err)
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(imgcheck.pixels_equal(
            self.original, Path(row["destination"]).read_bytes(), "png",
            allow_dirty_transparent=True))

    def test_doctor_shows_the_windows_commands(self):
        code, out, err = support.run_cli(
            ["--doctor"], tools_dir=self.tools, config=self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("truepng", out)
        self.assertIn("-zm5-9", out)      # диапазон, включающий скрейп
        self.assertIn("-zm8", out)        # фиксированный уровень для Advanced
        self.assertIn("deflopt -k", out)
        self.assertIn("-md remove all", out)

    def test_failing_truepng_leaves_the_original_untouched(self):
        """Провал обязательного шага не должен затрагивать исходник.

        Прогон идёт с набором без oxipng: иначе гонка подхватила бы второго
        кандидата, и проверить поведение при полном провале было бы нельзя.
        """
        only = support.install_fake_tools(self.base / "no_oxipng",
                                         only=["truepng", "deflopt", "advdef"])
        out = self.base / "fail_out"
        code, stdout, err = support.run_cli(
            ["/png:1", "/jpg:0", "/gif:0", "/outdir:%s" % out, "--tsv",
             str(self.images)],
            tools_dir=only, config=self.config,
            ICATALYST_FAKE_MODE="fail", ICATALYST_FAKE_TARGET="truepng")
        row = support.tsv_rows(stdout)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.png.read_bytes(), self.original)

    def test_race_saves_the_file_when_truepng_fails(self):
        """А при доступном oxipng формат не отваливается целиком."""
        code, row, err = self._run(1, ICATALYST_FAKE_MODE="fail",
                                   ICATALYST_FAKE_TARGET="truepng")
        self.assertEqual(code, 0, err)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["chain"], "oxipng")
        self.assertEqual(self.png.read_bytes(), self.original)

    def test_deflopt_failure_is_survivable(self):
        code, row, err = self._run(1, ICATALYST_FAKE_MODE="fail",
                                   ICATALYST_FAKE_TARGET="deflopt")
        self.assertEqual(code, 0, err)
        self.assertEqual(row["status"], "ok")


class CandidateRaceTest(unittest.TestCase):
    """oxipng участвует в гонке и может только улучшить результат."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.config = _windows_config(self.base)
        self.images = self.base / "img"
        self.images.mkdir()
        (self.images / "a.png").write_bytes(
            corpus.png_bytes(width=64, height=64, text=b"m" * 400))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, tools, **env):
        out = self.base / ("out%d" % len(list(self.base.iterdir())))
        code, stdout, err = support.run_cli(
            ["/png:1", "/jpg:0", "/gif:0", "/outdir:%s" % out, "--tsv",
             str(self.images)],
            tools_dir=tools, config=self.config, **env)
        rows = support.tsv_rows(stdout)
        return code, (rows[0] if rows else None), err

    def test_chain_absent_without_the_tool(self):
        """Без oxipng цепочка просто не участвует, а не роняет прогон."""
        only = support.install_fake_tools(self.base / "no_oxipng",
                                         only=["truepng", "deflopt", "advdef"])
        code, row, err = self._run(only)
        self.assertEqual(code, 0, err)
        self.assertEqual(row["chain"], "truepng")

    def test_race_picks_the_smaller_result(self):
        both = support.install_fake_tools(self.base / "both")
        # Подделка truepng «ломается» на этом файле, значит победить обязан
        # oxipng, а не весь формат — отвалиться.
        code, row, err = self._run(both, ICATALYST_FAKE_MODE="equal",
                                   ICATALYST_FAKE_TARGET="truepng")
        self.assertEqual(code, 0, err)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["chain"], "oxipng")
        self.assertLess(int(row["optimized"]), int(row["original"]))

    def test_oxipng_appears_in_doctor(self):
        both = support.install_fake_tools(self.base / "doc")
        code, out, err = support.run_cli(["--doctor"], tools_dir=both,
                                         config=self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("гонка 2 цепочек", out)
        self.assertIn("oxipng --quiet -o 4", out)
        # Возможности, объявленные подделкой, должны включить соответствующие
        # флаги — и только они.
        self.assertIn("--strip all", out)
        self.assertIn("-Z", out)


class PngwolfArgvTest(unittest.TestCase):
    """Скрейп zc/zm/zs и выбор формы командной строки pngwolf."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.tools = support.install_fake_tools(self.base / "fakebin")
        self.config = _windows_config(self.base)
        self.images = self.base / "img"
        self.images.mkdir()
        (self.images / "a.png").write_bytes(
            corpus.png_bytes(width=48, height=48, text=b"m" * 300))

    def tearDown(self):
        self._tmp.cleanup()

    def _doctor(self, **env):
        code, out, err = support.run_cli(["--doctor"], tools_dir=self.tools,
                                         config=self.config, **env)
        self.assertEqual(code, 0, err)
        return out

    def test_new_cli_form_is_used_for_version_1_1(self):
        out = self._doctor(ICATALYST_FAKE_PNGWOLF_CLI="new")
        self.assertIn("--out-deflate=zopfli,iter=", out)
        self.assertIn("--estimator=zlib,level=", out)
        self.assertNotIn("--zopfli-iter=", out)

    def test_legacy_cli_form_is_used_for_version_1_0(self):
        out = self._doctor(ICATALYST_FAKE_PNGWOLF_CLI="legacy")
        self.assertIn("--zopfli-iter=", out)
        self.assertIn("--zlib-level=", out)
        self.assertNotIn("--out-deflate=", out)

    def test_both_forms_actually_run(self):
        """Подделка отвергает форму, не соответствующую своей версии."""
        for form in ("new", "legacy"):
            with self.subTest(form=form):
                out_dir = self.base / ("o_%s" % form)
                code, stdout, err = support.run_cli(
                    ["/png:2", "/jpg:0", "/gif:0", "/outdir:%s" % out_dir,
                     "--tsv", str(self.images)],
                    tools_dir=self.tools, config=self.config,
                    ICATALYST_FAKE_PNGWOLF_CLI=form)
                self.assertEqual(code, 0, err)
                row = support.tsv_rows(stdout)[0]
                self.assertEqual(row["status"], "ok", row)

    def test_strategy_above_one_is_clamped_and_iterations_raised(self):
        """Правило из `iCatalyst.bat:729`: zs>1 → zs=1, а итераций 10 → 15."""
        from icatalyst.recipes import _parse_truepng_log, _pngwolf_args

        class _Proc:
            stdout = b"  zc: 7  zm: 9  zs: 3\n"

        class _Ctx:
            fmt, mode = "png", 2
            cfg = cfgmod.Config()
            work = Path("in.png")
            out = Path("out.png")
            scratch = {}

            class tools:
                @staticmethod
                def find(_name):
                    return None

        ctx = _Ctx()
        _parse_truepng_log(ctx, _Proc())
        self.assertEqual(ctx.scratch["zc"], 7)
        self.assertEqual(ctx.scratch["zm"], 9)
        self.assertEqual(ctx.scratch["zs"], 1)
        self.assertEqual(ctx.scratch["iterations"], 15)
        argv = _pngwolf_args(ctx)
        self.assertIn("--zopfli-iter=15", argv)
        self.assertIn("--zlib-strategy=1", argv)

    def test_strategy_zero_or_one_keeps_ten_iterations(self):
        from icatalyst.recipes import _parse_truepng_log

        class _Proc:
            stdout = b"  zc: 9  zm: 8  zs: 1\n"

        class _Ctx:
            scratch = {}

        ctx = _Ctx()
        _parse_truepng_log(ctx, _Proc())
        self.assertEqual(ctx.scratch["zs"], 1)
        self.assertEqual(ctx.scratch["iterations"], 10)

    def test_unparsable_log_falls_back_to_defaults(self):
        from icatalyst.recipes import _parse_truepng_log

        class _Proc:
            stdout = b"nothing useful here\n"

        class _Ctx:
            scratch = {}

        ctx = _Ctx()
        _parse_truepng_log(ctx, _Proc())
        self.assertEqual(ctx.scratch, {})


if __name__ == "__main__":
    unittest.main()


class ModeMonotonicityTest(unittest.TestCase):
    """Структурная проверка инварианта «Xtreme не хуже Advanced».

    Настоящий провал в CI выглядел так: `advdef -z2` (libdeflate) из Advanced
    обыграл zopfli-кандидатов Xtreme на 16-битном сером изображении, и медленный
    режим выдал 314 байт против 313. Обещание держалось случайно, потому что
    наборы кандидатов не были вложены друг в друга.

    Сравниваются именно команды, а не имена цепочек: на Windows имена совпадают
    («truepng», «oxipng»), а флаги внутри разные, и проверка по именам ничего бы
    не поймала. Ни одного установленного инструмента тест не требует, поэтому
    ловит регресс сразу, а не когда сойдутся размеры на конкретной картинке.
    """

    class _Tool:
        def __init__(self, name):
            self.name = name
            self.path = Path(name)
            self.version = "0"

        def has(self, _cap):
            # Все возможности объявлены доступными: набор команд должен
            # сравниваться в максимальной комплектации.
            return True

    class _Tools:
        def find(self, name):
            return ModeMonotonicityTest._Tool(name)

        def available(self, *_names):
            return True

        def warn_once(self, *_args):
            pass

    class _Ctx:
        def __init__(self, fmt, mode, cfg, tools):
            self.fmt, self.mode, self.cfg, self.tools = fmt, mode, cfg, tools
            self.work = Path("IN.png")
            self.out = Path("OUT.png")
            self.src = self.work
            self.scratch = {}

    def _commands(self, mode: int, windows: bool):
        """Множество команд, которые режим способен выполнить."""
        cfg = cfgmod.Config()
        tools = self._Tools()
        recipe = recipes.build("png", mode, cfg, windows=windows)
        commands = set()
        for chain in recipe.chains:
            for step in chain.steps:
                ctx = self._Ctx("png", mode, cfg, tools)
                argv = step.argv(ctx)
                if argv is not None:
                    commands.add((step.tool, tuple(argv)))
        return commands

    def test_xtreme_can_run_everything_advanced_can(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                advanced = self._commands(1, windows)
                xtreme = self._commands(2, windows)
                missing = advanced - xtreme
                self.assertEqual(
                    missing, set(),
                    "Xtreme не умеет то, что умеет Advanced: %r" % sorted(missing))

    def test_xtreme_also_does_more(self):
        """Иначе «надмножество» вышло бы вырожденным: Xtreme = Advanced."""
        for windows in (False, True):
            with self.subTest(windows=windows):
                extra = self._commands(2, windows) - self._commands(1, windows)
                self.assertTrue(extra, "Xtreme ничем не отличается от Advanced")

    def test_advanced_has_no_zopfli_class_steps(self):
        """Advanced обязан остаться быстрым: zopfli — это уже Xtreme."""
        for windows in (False, True):
            with self.subTest(windows=windows):
                rendered = " ".join(" ".join(argv)
                                    for _tool, argv in self._commands(1, windows))
                for slow in ("-4", "--iterations=", "-Z", "-zm5-9", "-o7", "max"):
                    self.assertNotIn(slow, rendered,
                                     "в Advanced просочился медленный шаг")
