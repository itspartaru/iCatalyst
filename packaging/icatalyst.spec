# PyInstaller spec для iCatalyst.exe.
#
# Один файл (--onefile по смыслу), консольное приложение: пользователь бросает
# папку на иконку, видит меню и таблицу — ровно как в 2.7.
#
# Вложенные Tools/apps/*.exe в exe НЕ упаковываются намеренно. Они остаются
# рядом с ним, потому что так exe остаётся около десяти мегабайт, антивирусы
# спокойнее относятся к нему без десятка чужих исполняемых файлов внутри, а
# обновление одного инструмента не требует пересборки. `Toolbox` ищет их в
# `Tools/apps` рядом с программой и, если она заморожена, дополнительно в
# `sys._MEIPASS`.
#
# tkinter, наоборот, нужен внутри: на Windows он даёт нативный диалог выбора
# папки, то есть заменяет закрытый browsefolder.exe без лишних бинарников.

import os
import sys

block_cipher = None

# Один spec на обе платформы. Имя различается намеренно: на Windows пользователь
# видит iCatalyst.exe и бросает на него папку, а на Linux имя должно совпадать с
# именем команды из .desktop-файла и из пакета.
IS_WINDOWS = sys.platform.startswith("win")
NAME = "iCatalyst" if IS_WINDOWS else "icatalyst"

analysis = Analysis(
    # Точка входа — run_icatalyst.py, а НЕ icatalyst/__main__.py: PyInstaller
    # исполняет входной скрипт как модуль `__main__` без родительского пакета, и
    # относительный импорт `from .cli import run` в нём падает с ImportError.
    # В run_icatalyst.py импорт абсолютный, поэтому он работает и в собранном
    # виде, и из распакованной папки.
    ["../run_icatalyst.py"],
    pathex=[".."],
    binaries=[],
    datas=[],
    # tkinter нужен внутри только на Windows: там он даёт нативный диалог выбора
    # папки, то есть заменяет закрытый browsefolder.exe. На Linux диалог берётся
    # из zenity или kdialog, а python3-tk на стоковых Ubuntu и Mint вообще не
    # установлен — тянуть его в бинарник незачем.
    hiddenimports=(["tkinter", "tkinter.filedialog"] if IS_WINDOWS else []),
    hookspath=[],
    runtime_hooks=[],
    # Рантайм ядра — только стандартная библиотека, поэтому исключаем тяжёлое,
    # что PyInstaller мог бы затянуть по ошибке.
    excludes=(["numpy", "PIL", "pytest", "setuptools", "pip"]
              + ([] if IS_WINDOWS else ["tkinter"])),
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

icon = os.path.join("..", "Tools", "icon.ico")

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX выключен: он ломает подпись и повышает шанс ложного срабатывания
    # антивирусов, а выигрыш в размере здесь несущественен.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=(icon if IS_WINDOWS and os.path.exists(icon) else None),
)
