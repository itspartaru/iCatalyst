@echo off
rem Запуск нового ядра из исходников на Windows: бросьте на этот файл папку или
rem файлы. Рядом с собранным релизом эта обёртка не нужна — там iCatalyst.exe
rem сам является целью для drag & drop.
rem
rem Имя с суффиксом -3 выбрано намеренно: iCatalyst.bat пока остаётся рабочей
rem реализацией 2.7 и будет заменён этой обёрткой только после того, как работа
rem parity-windows в CI подтвердит, что новое ядро сжимает не хуже.

setlocal
set "ICROOT=%~dp0"

if exist "%ICROOT%iCatalyst.exe" (
    "%ICROOT%iCatalyst.exe" %*
    exit /b %errorlevel%
)

rem py.exe — штатный лончер Python на Windows; он находит нужную версию сам.
where /q py.exe && (
    py -3 "%ICROOT%run_icatalyst.py" %*
    exit /b %errorlevel%
)
where /q python.exe && (
    python "%ICROOT%run_icatalyst.py" %*
    exit /b %errorlevel%
)

echo.-------------------------------------------------------------------------------
echo. Python 3.9 or newer is required to run Image Catalyst from source.
echo.
echo. Install it from https://www.python.org/downloads/ and tick
echo. "Add python.exe to PATH", or download a release build of iCatalyst.exe:
echo. https://github.com/lorents17/iCatalyst/releases
echo.-------------------------------------------------------------------------------
pause
exit /b 2
