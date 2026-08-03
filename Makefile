# Тонкие обёртки над Tools/build_tools.py и unittest. Вся логика — в Python,
# чтобы не заводить в проекте второй язык и не спотыкаться о переводы строк:
# .sh, извлечённый с CRLF, умирает с "bad interpreter: /bin/bash^M".

PYTHON ?= python3

.PHONY: help tools tools-build check test test-real doctor exe deb binary clean apt

help:
	@echo "make tools       - скачать готовые сборки инструментов"
	@echo "make tools-build - собрать mozjpeg и pngwolf из исходников"
	@echo "make apt         - показать строку установки системных пакетов"
	@echo "make check       - показать найденные инструменты"
	@echo "make doctor      - показать инструменты и точные команды по режимам"
	@echo "make test        - прогнать тесты (без установленных оптимизаторов тоже)"
	@echo "make test-real   - только тесты с настоящими инструментами"
	@echo "make exe         - собрать iCatalyst.exe (только Windows)"
	@echo "make deb         - собрать .deb и проверить его lintian"
	@echo "make binary      - собрать самодостаточный бинарник для Linux"
	@echo "make clean       - удалить Tools/build и артефакты сборки"

tools:
	$(PYTHON) Tools/build_tools.py --download

tools-build:
	$(PYTHON) Tools/build_tools.py --build

apt:
	@$(PYTHON) Tools/build_tools.py --print-apt

check:
	$(PYTHON) Tools/build_tools.py --check

doctor:
	$(PYTHON) -m icatalyst --doctor

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

test-real:
	$(PYTHON) -m unittest tests.test_real_tools -v

exe:
	$(PYTHON) -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/icatalyst.spec

deb:
	$(PYTHON) packaging/build_deb.py --lintian

# PyInstaller — зависимость времени сборки, а не рантайма, поэтому ставится в
# отдельное окружение: на Ubuntu 24.04 и новее установка в системный Python
# запрещена (PEP 668), и это правильно.
binary:
	$(PYTHON) -m venv build/venv
	build/venv/bin/pip install --quiet --disable-pip-version-check "pyinstaller==6.*"
	build/venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build/pyi packaging/icatalyst.spec
	./dist/icatalyst --version

clean:
	$(PYTHON) Tools/build_tools.py --clean
	rm -rf build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
