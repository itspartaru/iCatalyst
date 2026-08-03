"""Проверки геометрии таблицы и форматирования чисел.

Ширины колонок в 2.7 были зашиты в десятке мест (`iCatalyst.bat:221-222`,
`:392-410`, `:1176-1194`), и любое расхождение сдвигало вывод. Этот файл
закрепляет их эталоном.
"""

from __future__ import annotations

import io
import unittest

from icatalyst import report
from icatalyst.pipeline import STATUS_OK, Job, Result


class SizeFormatTest(unittest.TestCase):
    def test_bytes_are_printed_as_integers(self):
        self.assertEqual(report.format_size(0), "0 B")
        self.assertEqual(report.format_size(999), "999 B")

    def test_larger_units_are_truncated_not_rounded(self):
        # 1023.99 KB не должно превратиться в 1024.00 KB.
        self.assertEqual(report.format_size(1024), "1.00 KB")
        self.assertEqual(report.format_size(1536), "1.50 KB")
        self.assertEqual(report.format_size(1024 * 1024 - 1), "1023.99 KB")
        self.assertEqual(report.format_size(1024 * 1024), "1.00 MB")
        self.assertEqual(report.format_size(3 * 1024 ** 3), "3.00 GB")

    def test_negative_values_keep_the_sign(self):
        # Выигрыш в 2.7 печатался отрицательным числом: change = new - original.
        self.assertEqual(report.format_size(-2048), "-2.00 KB")
        self.assertEqual(report.format_size(-15), "-15 B")

    def test_forced_unit_matches_the_original_column(self):
        unit = report.pick_unit(5 * 1024 * 1024)
        self.assertEqual(report.format_size(1024, unit), "0.00 MB")

    def test_totals_beyond_32_bit_are_correct(self):
        """В 2.7 итоги свыше 2 ГБ приходилось масштабировать вручную."""
        self.assertEqual(report.format_size(9 * 1024 ** 4), "9.00 TB")


class PercentTest(unittest.TestCase):
    def test_sign_and_truncation(self):
        self.assertEqual(report.format_percent(-500, 1000), "-50.00%")
        self.assertEqual(report.format_percent(-1, 3), "-33.33%")
        self.assertEqual(report.format_percent(0, 1000), "0.00%")

    def test_zero_original_does_not_divide_by_zero(self):
        self.assertEqual(report.format_percent(-10, 0), "0.00%")


class WidthTest(unittest.TestCase):
    def test_cjk_and_emoji_count_two_columns(self):
        self.assertEqual(report.display_width("abc"), 3)
        self.assertEqual(report.display_width("日本語"), 6)
        self.assertEqual(report.display_width("é"), 1)

    def test_crop_keeps_extension(self):
        name = "очень_длинное_имя_файла_которое_не_влезает.png"
        cropped = report.crop_filename(name, 31)
        self.assertTrue(cropped.endswith("..png"))
        self.assertLessEqual(report.display_width(cropped), 31)

    def test_short_names_are_untouched(self):
        self.assertEqual(report.crop_filename("логотип.png", 31), "логотип.png")


class TableGeometryTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.reporter = report.Reporter(stream=self.out, err=io.StringIO(),
                                        width=report.RULE_WIDTH, use_title=False)

    def test_header_widths(self):
        self.reporter.header()
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines[0]), 79)
        self.assertEqual(len(lines[1]), 78)
        # Вторая строка шапки в 2.7 обрывается после четвёртого разделителя:
        # колонка «% Savings» во второй строке пуста, и хвостовых пробелов там
        # нет (`iCatalyst.bat:222`).
        self.assertEqual(len(lines[2]), 68)
        self.assertEqual(lines[2].count("|"), 4)
        self.assertTrue(lines[2].endswith("|"))
        self.assertEqual(lines[1].split("|")[0], " " + "File Name".ljust(31))
        # Ровно пять колонок и четыре разделителя.
        self.assertEqual(lines[1].count("|"), 4)

    def _row(self, name, orig, new):
        job = Job(src=__import__("pathlib").Path(name), root=None, fmt="png", mode=1)
        return Result(job, None, orig, new, STATUS_OK)

    def test_row_width_matches_header(self):
        for name, orig, new in [
            ("логотип.png", 100000, 90000),
            ("日本語のファイル名.png", 5, 4),
            ("emoji🎉.png", 1024 ** 3, 1024 ** 3 - 1),
            ("очень_длинное_имя_файла_которое_точно_не_влезёт.png", 999, 999),
        ]:
            self.out.seek(0)
            self.out.truncate()
            self.reporter.row(self._row(name, orig, new))
            line = self.out.getvalue().rstrip("\n")
            self.assertEqual(report.display_width(line), 78,
                             "имя %r даёт ширину %d" % (name, report.display_width(line)))

    def test_totals_row_width(self):
        summary = report.Summary()
        bucket = summary.bucket("png")
        bucket.label = "Xtreme"
        bucket.total_files = 10
        bucket.done_files = 10
        bucket.original = 5 * 1024 * 1024
        bucket.optimized = 4 * 1024 * 1024
        self.reporter.totals(summary)
        rows = [l for l in self.out.getvalue().splitlines() if l.startswith(" PNG")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(report.display_width(rows[0]), 78)
        self.assertIn("PNG [10/10]:", rows[0])

    def test_narrow_terminal_shrinks_only_the_name_column(self):
        narrow = report.Reporter(stream=io.StringIO(), err=io.StringIO(),
                                 width=60, use_title=False)
        self.assertLess(narrow.name_w, report.NAME_W)
        self.assertGreaterEqual(narrow.name_w, 12)
        self.assertEqual(len(narrow.rule), narrow.row_width + 1)


class TitleTest(unittest.TestCase):
    def test_progress_title_format(self):
        title = report.progress_title({
            "PNG": ("Xtreme", 42, 100),
            "JPG": ("Baseline", 1, 10),
        }, "Image Catalyst")
        self.assertEqual(title, "[PNG Xtreme: 42% | JPG Baseline: 10%] Image Catalyst")

    def test_percent_is_floored_like_set_a(self):
        title = report.progress_title({"GIF": ("Default", 2, 3)}, "x")
        self.assertIn("66%", title)

    def test_empty_progress_has_no_brackets(self):
        self.assertEqual(report.progress_title({}, "Image Catalyst"), "Image Catalyst")


class TsvTest(unittest.TestCase):
    def test_decoration_is_suppressed(self):
        out = io.StringIO()
        reporter = report.Reporter(stream=out, err=io.StringIO(), tsv=True,
                                   use_title=False)
        reporter.header()
        reporter.notes(["это не должно попасть в машинный вывод"])
        reporter.decoration("-" * 79)
        reporter.totals(report.Summary())
        lines = out.getvalue().splitlines()
        self.assertEqual(lines, ["status\tformat\tchain\tsource\tdestination"
                                 "\toriginal\toptimized\tmessage"])

    def test_fields_with_tabs_and_newlines_are_escaped(self):
        """Имена файлов на Linux содержат и табуляцию, и перевод строки."""
        out = io.StringIO()
        reporter = report.Reporter(stream=out, err=io.StringIO(), tsv=True,
                                   use_title=False)
        reporter.tsv_row("ok", "png", "chain", "/tmp/a\tb\nc\\d", "", "1", "1", "")
        line = out.getvalue().rstrip("\n")
        self.assertEqual(len(line.split("\t")), 8)
        self.assertEqual(report.unescape_tsv(line.split("\t")[3]), "/tmp/a\tb\nc\\d")


if __name__ == "__main__":
    unittest.main()
