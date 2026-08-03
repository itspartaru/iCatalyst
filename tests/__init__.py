"""Набор тестов Image Catalyst.

Потоки вывода настраиваются здесь же, при импорте пакета тестов. На
windows-раннере stdout — cp1252, и `print()` русского текста падает с
UnicodeEncodeError: именно так упал харнесс паритета, печатавший таблицу
сравнения размеров. Приложение решает это в `report.configure_streams()`, и
тесты, которые печатают не меньше русского, обязаны делать то же.
"""

from icatalyst.report import configure_streams as _configure_streams

_configure_streams()
