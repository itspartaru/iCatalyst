"""Image Catalyst — сжатие PNG, JPEG и GIF без потерь.

Единственный источник версии в проекте. Раньше версия дублировалась в
`iCatalyst.bat` (`set "version=2.7"`), в баннере `:helpmsg` и в двух README —
и, разумеется, разъезжалась.
"""

__version__ = "3.0.0.dev0"

APP_NAME = "Image Catalyst"

#: Ширина разделительной линии в отчёте. Взята из `iCatalyst.bat:15` дословно:
#: любое изменение сдвигает всю таблицу.
RULE_WIDTH = 79


def app_directory():
    """Каталог, рядом с которым лежат `Tools/` и `config.ini`.

    Для обычного запуска это корень репозитория или каталог установки. Для
    сборки PyInstaller — каталог самого исполняемого файла, а НЕ `sys._MEIPASS`:
    вложенные инструменты и настройки намеренно лежат рядом с exe, а не внутри
    него, иначе обновление одного инструмента требовало бы пересборки.
    """
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
