@echo off
setlocal

REM === Переключаем консоль на UTF-8 ===
chcp 65001 >nul

REM === Переходим в папку со скриптом ===
cd /d "%~dp0"

REM === Проверка наличия Python ===
where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Скачиваю и устанавливаю...
    powershell -Command "Invoke-WebRequest -Uri https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe -OutFile python_installer.exe"
    echo Устанавливаю Python...
    start /wait python_installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
    del python_installer.exe
)

REM === Создание виртуального окружения ===
if not exist ".venv" (
    echo Создаю виртуальное окружение...
    python -m venv .venv
)

REM === Активация окружения ===
call .venv\Scripts\activate.bat

REM === Обновление pip ===
echo Обновляю pip...
python -m pip install --upgrade pip

REM === Установка зависимостей ===
echo Устанавливаю библиотеки...
pip install selenium webdriver-manager colorama requests

REM === Запуск main.py ===
echo.
echo Запуск main.py...
python main.py

echo.
echo Завершено.
pause
