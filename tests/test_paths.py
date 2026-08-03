"""Проверки отображения выходных путей — там, где в `filter.js` жили баги."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from icatalyst.paths import CannotNameOutput, OutputMapper, long_path, normkey


class MapperTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # resolve(): на Windows tempfile отдаёт короткую форму 8.3, а приложение
        # нормализует пути — без приведения к одной форме сравнения расходятся.
        self.base = Path(self._tmp.name).resolve()
        self.src_dir = self.base / "Фото"
        (self.src_dir / "вложенная").mkdir(parents=True)
        self.file_a = self.src_dir / "лого.png"
        self.file_b = self.src_dir / "вложенная" / "лого.png"
        for path in (self.file_a, self.file_b):
            path.write_bytes(b"x")
        self.out = self.base / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_in_place_returns_the_source(self):
        mapper = OutputMapper(None)
        self.assertTrue(mapper.in_place)
        self.assertEqual(mapper.destination(self.file_a, self.src_dir), self.file_a)

    def test_directory_tree_is_mirrored(self):
        mapper = OutputMapper(self.out)
        self.assertEqual(mapper.destination(self.file_a, self.src_dir),
                         self.out / "Фото" / "лого.png")
        self.assertEqual(mapper.destination(self.file_b, self.src_dir),
                         self.out / "Фото" / "вложенная" / "лого.png")

    def test_single_file_root_lands_in_outdir(self):
        mapper = OutputMapper(self.out)
        self.assertEqual(mapper.destination(self.file_a, self.file_a),
                         self.out / "лого.png")

    def test_existing_destination_gets_a_suffix(self):
        (self.out / "лого.png").write_bytes(b"already here")
        mapper = OutputMapper(self.out)
        self.assertEqual(mapper.destination(self.file_a, self.file_a),
                         self.out / "лого-0001.png")

    def test_collision_inside_one_run_does_not_overwrite(self):
        """Главный дефект `filter.js:110`: проверялся только уже существующий файл.

        Два входных файла с одинаковым именем, поданных как отдельные аргументы,
        оба получали имя без суффикса, и второй молча затирал первый.
        """
        mapper = OutputMapper(self.out)
        first = mapper.destination(self.file_a, self.file_a)
        second = mapper.destination(self.file_b, self.file_b)
        self.assertNotEqual(first, second)
        self.assertEqual(first, self.out / "лого.png")
        self.assertEqual(second, self.out / "лого-0001.png")

    def test_repeated_directory_root_gets_a_suffix(self):
        other = self.base / "другое" / "Фото"
        other.mkdir(parents=True)
        third = other / "лого.png"
        third.write_bytes(b"x")
        mapper = OutputMapper(self.out)
        mapper.destination(self.file_a, self.src_dir)
        self.assertEqual(mapper.destination(third, other),
                         self.out / "Фото-0001" / "лого.png")

    def test_outdir_equal_to_parent_means_in_place(self):
        """Пофайловое, а не глобальное правило — как в `filter.js:91`.

        Правило сравнивает каталог назначения именно с каталогом самого файла,
        поэтому при `outdir`, совпадающем с входным корнем, файлы из корня
        обрабатываются на месте, а файлы из подкаталогов уезжают во вложенный
        `Фото/Фото/...`. Поведение странное, но именно такое было в 2.7, и
        менять его молча нельзя: альтернатива — затирать разные файлы одним
        именем.
        """
        mapper = OutputMapper(self.src_dir)
        self.assertEqual(mapper.destination(self.file_a, self.src_dir), self.file_a)
        self.assertEqual(mapper.destination(self.file_b, self.src_dir),
                         self.src_dir / "Фото" / "вложенная" / "лого.png")

    def test_suffix_padding_switches_to_plain_numbers(self):
        """После 9999 нумерация продолжается, а не обрывается ошибкой.

        В 2.7 `getFileName` возвращал пустую строку, и путь попадал в корзину
        «Images with characters» — неверное сообщение в неверной корзине.
        """
        mapper = OutputMapper(self.out)
        target = self.out / "a.png"
        mapper._reserved.add(normkey(target))
        for i in range(1, 10000):
            mapper._reserved.add(normkey(self.out / ("a-%04d.png" % i)))
        result = mapper._free_name(target)
        self.assertEqual(result.name, "a-10000.png")

    def test_names_differing_only_in_case(self):
        upper = self.src_dir / "Лого.png"
        upper.write_bytes(b"x")
        mapper = OutputMapper(self.out)
        first = mapper.destination(self.file_a, self.src_dir)
        second = mapper.destination(upper, self.src_dir)
        if os.name == "nt" or normkey("A") == normkey("a"):
            # Регистронезависимая ФС: второй файл обязан получить суффикс.
            self.assertNotEqual(normkey(first), normkey(second))
        else:
            self.assertEqual(second, self.out / "Фото" / "Лого.png")


class LongPathTest(unittest.TestCase):
    def test_short_paths_are_untouched(self):
        self.assertEqual(long_path(Path("/tmp/a")), str(Path("/tmp/a")))

    @unittest.skipUnless(os.name == "nt", "префикс \\\\?\\ существует только в Windows")
    def test_windows_prefix_is_added(self):
        deep = Path("C:\\" + "\\".join("d" * 20 for _ in range(20)))
        self.assertTrue(long_path(deep).startswith("\\\\?\\"))


if __name__ == "__main__":
    unittest.main()
