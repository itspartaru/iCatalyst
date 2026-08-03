#!/usr/bin/env python3
"""Сборка .deb-пакета без debhelper — только `dpkg-deb` и стандартная библиотека.

Пакет получается `Architecture: all`, потому что ядро — чистый Python. Сами
оптимизаторы не вкладываются: они есть в репозиториях дистрибутива, и объявить их
в `Depends` честнее и правильнее, чем тащить чужие бинарники внутри своего
пакета.

Почему не debhelper: он потребовал бы `debian/rules`, `dh_*` и знания их
соглашений ради пакета из десятка чистых Python-файлов. `dpkg-deb --build`
делает то же самое, а весь состав пакета виден в одном файле.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from icatalyst import __version__  # noqa: E402

PACKAGE = "icatalyst"
MAINTAINER = "Lorents & Res2001 <noreply@github.com>"
HOMEPAGE = "https://github.com/lorents17/iCatalyst"

#: Без этих пакетов основные режимы не работают.
DEPENDS = [
    "python3 (>= 3.9)",
    "optipng",
    "gifsicle",
    "libjpeg-turbo-progs | libjpeg-progs",
]
#: Улучшают сжатие, но не обязательны: без них режим просто деградирует.
RECOMMENDS = ["zopfli", "advancecomp"]
#: Диалог выбора каталога. Без них остаётся запрос в терминале.
SUGGESTS = ["zenity", "kdialog", "libimage-exiftool-perl"]


def deb_version(version: str) -> str:
    """Привести версию Python к принятой в Debian.

    `3.0.0.dev0` в Debian сравнивается как **больше** `3.0.0`, поэтому
    предвыпускная часть записывается через тильду: `3.0.0~dev0` меньше
    `3.0.0`, и обновление до релиза проходит штатно.
    """
    match = re.match(r"^(\d+(?:\.\d+)*)[._-]?(a|b|rc|dev|alpha|beta)\.?(\d*)$",
                     version)
    if match:
        base, kind, number = match.groups()
        return "%s~%s%s" % (base, kind, number or "0")
    return version


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _write_gz(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0: иначе один и тот же исходник давал бы разные байты пакета.
    with gzip.GzipFile(str(path), "wb", mtime=0) as fh:
        fh.write(text.encode("utf-8"))
    path.chmod(0o644)


def _installed_size(root: Path) -> int:
    total = 0
    for base, _dirs, names in os.walk(root):
        if Path(base).name == "DEBIAN":
            continue
        for name in names:
            total += (Path(base) / name).stat().st_size
    return max(1, total // 1024)


def build_tree(root: Path, version: str) -> None:
    site = root / "usr/lib/python3/dist-packages" / PACKAGE
    site.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "icatalyst").glob("*.py")):
        shutil.copyfile(source, site / source.name)
        (site / source.name).chmod(0o644)

    # Запускалка. `python3 -m icatalyst` работает, потому что пакет лежит в
    # dist-packages; отдельная обёртка нужна лишь для того, чтобы имя команды и
    # `Exec=` из .desktop-файла совпадали.
    _write(root / "usr/bin" / PACKAGE,
           "#!/bin/sh\nexec python3 -m icatalyst \"$@\"\n", mode=0o755)

    desktop = (REPO_ROOT / "icatalyst.desktop").read_text(encoding="utf-8")
    _write(root / "usr/share/applications" / ("%s.desktop" % PACKAGE), desktop)

    # config.ini — conffile: dpkg не станет затирать правки пользователя при
    # обновлении пакета.
    shutil.copyfile(REPO_ROOT / "Tools" / "config.ini",
                    _ensure(root / "etc" / PACKAGE / "config.ini"))
    (root / "etc" / PACKAGE / "config.ini").chmod(0o644)

    doc = root / "usr/share/doc" / PACKAGE
    _write(doc / "copyright", _copyright())
    # Пакет native (без отдельной upstream-версии), поэтому changelog.gz,
    # а не changelog.Debian.gz — этого требует политика Debian.
    _write_gz(doc / "changelog.gz", _changelog(version))
    for name in ("README.md", "README.RU.md", "THIRD-PARTY.md"):
        source = REPO_ROOT / name
        if source.is_file():
            shutil.copyfile(source, doc / name)
            (doc / name).chmod(0o644)

    _write_gz(root / "usr/share/man/man1" / ("%s.1.gz" % PACKAGE), _manpage(version))

    control = root / "DEBIAN"
    control.mkdir(parents=True, exist_ok=True)
    _write(control / "control", _control(version, _installed_size(root)))
    _write(control / "conffiles", "/etc/%s/config.ini\n" % PACKAGE)

    # Каталоги создавались с правами по umask (обычно 0775), а политика Debian
    # требует 0755. Разница не косметическая: 0775 даёт запись группе.
    for base, dirs, _names in os.walk(root):
        for name in dirs:
            (Path(base) / name).chmod(0o755)
    root.chmod(0o755)


def _ensure(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _control(version: str, size_kb: int) -> str:
    return (
        "Package: %s\n"
        "Version: %s\n"
        "Architecture: all\n"
        "Maintainer: %s\n"
        "Installed-Size: %d\n"
        "Depends: %s\n"
        "Recommends: %s\n"
        "Suggests: %s\n"
        "Section: graphics\n"
        "Priority: optional\n"
        "Homepage: %s\n"
        "Description: lossless PNG, JPEG and GIF image optimizer\n"
        " Image Catalyst recompresses images without touching a single pixel.\n"
        " It orchestrates well-known optimizers, races several tool chains against\n"
        " each other and keeps whichever result is smallest, never accepting a file\n"
        " that is not strictly smaller than the input.\n"
        " .\n"
        " Drop a folder onto its icon and it asks how to compress each format that\n"
        " is actually present, then writes recompressed copies to the directory you\n"
        " choose. It can also be scripted from the command line.\n"
        % (PACKAGE, version, MAINTAINER, size_kb, ", ".join(DEPENDS),
           ", ".join(RECOMMENDS), ", ".join(SUGGESTS), HOMEPAGE)
    )


def _copyright() -> str:
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: Image Catalyst\n"
        "Source: %s\n"
        "\n"
        "Files: *\n"
        "Copyright: 2010-2026 Lorents, Res2001\n"
        "License: MIT\n"
        "\n"
        "License: MIT\n"
        " Permission is hereby granted, free of charge, to any person obtaining a\n"
        " copy of this software and associated documentation files (the \"Software\"),\n"
        " to deal in the Software without restriction, including without limitation\n"
        " the rights to use, copy, modify, merge, publish, distribute, sublicense,\n"
        " and/or sell copies of the Software, and to permit persons to whom the\n"
        " Software is furnished to do so, subject to the following conditions:\n"
        " .\n"
        " The above copyright notice and this permission notice shall be included\n"
        " in all copies or substantial portions of the Software.\n"
        " .\n"
        " THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS\n"
        " OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF\n"
        " MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.\n"
        " IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY\n"
        " CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,\n"
        " TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE\n"
        " SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.\n"
        "\n"
        "Comment: The optimizers this package depends on are separate programs with\n"
        " their own licenses; they are not distributed here. See THIRD-PARTY.md.\n"
        % HOMEPAGE
    )


def _changelog(version: str) -> str:
    stamp = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(
        int(os.environ.get("SOURCE_DATE_EPOCH", "0")) or None))
    return (
        "%s (%s) unstable; urgency=low\n"
        "\n"
        "  * Python core replacing the Windows batch implementation.\n"
        "  * Linux support.\n"
        "  * Files whose names contain national characters are no longer skipped.\n"
        "\n"
        " -- %s  %s\n"
        % (PACKAGE, version, MAINTAINER, stamp)
    )


def _manpage(version: str) -> str:
    return r""".TH ICATALYST 1 "Image Catalyst %s" "icatalyst" "User Commands"
.SH NAME
icatalyst \- lossless PNG, JPEG and GIF image optimizer
.SH SYNOPSIS
.B icatalyst
[\fIoptions\fR] [\fIdirectories\fR | \fIfiles\fR]
.SH DESCRIPTION
Recompresses images without altering pixels. Several tool chains are raced
against each other and the smallest result wins; a result that is not strictly
smaller than the input is discarded and the original is kept.
.PP
If a mode is not given on the command line, it is asked for interactively, but
only for formats actually present in the input.
.SH OPTIONS
.TP
.BI /png: N
PNG mode: 1 Advanced, 2 Xtreme, 0 skip.
.TP
.BI /jpg: N
JPEG mode: 1 baseline, 2 progressive, 3 keep the original encoding, 0 skip.
.TP
.BI /gif: N
GIF mode: 1 keep the original settings, 0 skip.
.TP
.BI /outdir: VALUE
Where to write results: \fBtrue\fR to ask, \fBfalse\fR to replace the originals,
or a directory path.
.TP
.B \-\-verify
Compare PNG pixel data before and after. Slow but thorough.
.TP
.B \-\-strict\-lossless
Forbid rewriting RGB values underneath fully transparent pixels.
.TP
.BI \-\-threads " N"
Number of parallel jobs. Defaults to the CPU count.
.TP
.B \-\-tsv
Machine-readable output instead of the table.
.TP
.B \-\-doctor
Report which optimizers were found and the exact commands that will be run.
.SH FILES
.TP
.I ~/.config/icatalyst/config.ini
Per-user configuration; overrides the system one.
.TP
.I /etc/icatalyst/config.ini
System-wide configuration.
.SH EXIT STATUS
0 success, 1 some images failed, 2 bad usage or configuration,
130 interrupted.
.SH SEE ALSO
.BR optipng (1),
.BR gifsicle (1),
.BR jpegtran (1),
.BR advdef (1),
.BR zopflipng (1)
.SH BUGS
%s/issues
""" % (version, HOMEPAGE)


def build(output: Optional[Path] = None) -> Path:
    version = deb_version(__version__)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        build_tree(root, version)
        name = "%s_%s_all.deb" % (PACKAGE, version)
        target = (output or REPO_ROOT / "dist") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = ["dpkg-deb", "--root-owner-group", "--build", str(root), str(target)]
        if shutil.which("fakeroot") and os.geteuid() != 0:
            # --root-owner-group уже делает нужное, fakeroot оставлен для
            # совместимости со старыми dpkg, где этого ключа нет.
            pass
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout.decode("utf-8", "replace"))
            raise SystemExit("dpkg-deb завершился с кодом %d" % proc.returncode)
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка .deb-пакета Image Catalyst")
    parser.add_argument("--output", type=Path, default=None,
                        help="каталог для результата (по умолчанию dist/)")
    parser.add_argument("--lintian", action="store_true",
                        help="прогнать lintian по собранному пакету")
    args = parser.parse_args(argv)

    package = build(args.output)
    print("собран: %s (%d КБ)" % (package, package.stat().st_size // 1024))

    if args.lintian:
        if shutil.which("lintian") is None:
            print("lintian не установлен, проверка пропущена")
            return 0
        proc = subprocess.run(["lintian", "--no-tag-display-limit", str(package)])
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
