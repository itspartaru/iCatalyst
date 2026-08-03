"""Инфраструктура тестов: установка поддельных инструментов и запуск CLI."""

from __future__ import annotations

import io
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

from tests.faketool import TOOLS  # noqa: E402  (после вычисления REPO_ROOT)

#: Интерпретатор в shebang прописан абсолютным путём, а не через
#: `/usr/bin/env python3`: тесты подменяют PATH целиком, чтобы `shutil.which` не
#: находил установленные в системе диалоги, и тогда `env` было бы негде взять.
_POSIX_WRAPPER = """#!{python}
import sys
sys.path.insert(0, {repo!r})
from tests.faketool import main
sys.exit(main({name!r}, sys.argv[1:]))
"""

_WINDOWS_WRAPPER = """@echo off
"{python}" -c "import sys; sys.path.insert(0, r'{repo}'); from tests.faketool import main; sys.exit(main('{name}', sys.argv[1:]))" %*
"""


def install_fake_tools(dest: Path, only: Optional[Sequence[str]] = None) -> Path:
    """Разложить поддельные инструменты в каталог и вернуть его.

    Каталог передаётся приложению через `ICATALYST_TOOLS_DIR`, который в
    `Toolbox` стоит выше всех остальных мест поиска, кроме явного пути к
    конкретному инструменту.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    names = list(only) if only else list(TOOLS)
    for name in names:
        if os.name == "nt":
            target = dest / ("%s.cmd" % name)
            target.write_text(_WINDOWS_WRAPPER.format(
                python=sys.executable, repo=str(REPO_ROOT), name=name), encoding="utf-8")
        else:
            target = dest / name
            target.write_text(_POSIX_WRAPPER.format(
                python=sys.executable, repo=str(REPO_ROOT), name=name),
                encoding="utf-8")
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return dest


@contextmanager
def environment(**values: Optional[str]):
    """Временно выставить переменные окружения (None удаляет переменную)."""
    saved: Dict[str, Optional[str]] = {}
    try:
        for key, value in values.items():
            saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def captured():
    """Перехватить stdout, stderr и stdin, оставив их не-терминалами.

    Отсутствие isatty важно: отчёт не должен ни писать заголовок окна, ни
    ставить паузу, ни чистить экран, когда вывод перенаправлен. Подменяется и
    stdin — иначе результат зависел бы от того, запущен ли набор тестов из
    живого терминала, и меню могло бы ждать ввода.
    """
    out, err, inp = io.StringIO(), io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr, sys.stdin
    sys.stdout, sys.stderr, sys.stdin = out, err, inp
    try:
        yield out, err
    finally:
        sys.stdout, sys.stderr, sys.stdin = saved


def empty_config(directory: Path) -> Path:
    """Создать пустой config.ini.

    Тесты обязаны им пользоваться: иначе подхватится `Tools/config.ini` из
    репозитория, где `outdir=true`, и прогон полез бы спрашивать каталог.
    """
    path = Path(directory) / "test-config.ini"
    path.write_text("[options]\n", encoding="utf-8")
    return path


def run_cli(args: Sequence[str], tools_dir: Optional[Path] = None,
            config: Optional[Path] = None,
            **env: Optional[str]) -> Tuple[int, str, str]:
    """Прогнать CLI в текущем процессе и вернуть (код, stdout, stderr)."""
    from icatalyst import cli

    argv = list(args)
    if config is not None and not any(a == "--config" for a in argv):
        argv = ["--config", str(config)] + argv
    overrides: Dict[str, Optional[str]] = {
        "ICATALYST_TOOLS_DIR": str(tools_dir) if tools_dir else None,
        # Когда подставлены подделки, поиск не должен уходить в PATH: иначе
        # установленный в системе оптимизатор подменяет подделку, и тест,
        # проверяющий отсутствие инструмента, перестаёт что-либо проверять.
        "ICATALYST_TOOLS_ONLY": "1" if tools_dir else None,
        "ICATALYST_NO_TITLE": "1",
        "ICATALYST_CONFIG": None,
        # Безопасное значение по умолчанию: без него тест, забывший указать
        # /outdir, находит установленный в системе zenity и открывает настоящий
        # диалог, который ждёт человека. Тесты про графические диалоги
        # переопределяют эту переменную явно.
        "ICATALYST_PICKER": "terminal",
    }
    overrides.update(env)
    with environment(**overrides), captured() as (out, err):
        code = cli.run(argv)
    return code, out.getvalue(), err.getvalue()


def tsv_rows(stdout: str) -> List[Dict[str, str]]:
    """Разобрать вывод `--tsv` в список словарей."""
    from icatalyst.report import unescape_tsv

    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        fields = line.split("\t")
        # Строка с другим числом полей — не данные, а оформление, просочившееся
        # в машинный вывод. Тихо съедать такое нельзя, поэтому падаем.
        if len(fields) != len(header):
            raise AssertionError("не-TSV строка в машинном выводе: %r" % line)
        rows.append({key: unescape_tsv(value) for key, value in zip(header, fields)})
    return rows
