@echo off
chcp 65001 >nul
color 0A
title FunPay Bot - Zapusk

echo.
echo  =====================================================
echo             FunPay Bot  -  Launcher
echo  =====================================================
echo.

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"

if not exist "%BACKEND%\logs"   mkdir "%BACKEND%\logs"
if not exist "%BACKEND%\config" mkdir "%BACKEND%\config"

echo  [1/4] Proverka Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [OSHIBKA] Python ne najden!
    echo  Ustanovite Python 3.10+ s https://www.python.org/downloads/
    echo  Pri ustanovke otmetjte Add Python to PATH
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  [OK] %PY_VER%

echo  [2/4] Proverka zavisimostej Python...
python -c "import fastapi, uvicorn, requests, bs4, lxml, requests_toolbelt, websockets" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Ustanavlivayu zavisimosti...
    pip install -r "%BACKEND%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo  [OSHIBKA] Ne udalos ustanovit zavisimosti!
        echo  Poprobujte vruchnuyu: pip install -r backend\requirements.txt
        pause
        exit /b 1
    )
)
echo  [OK] Vse zavisimosti ustanovleny

echo  [2b] Proverka lokal'noj FunPayAPI...
if not exist "%BACKEND%\FunPayAPI\__init__.py" (
    echo  [OSHIBKA] Papka backend\FunPayAPI ne naydena!
    echo  Razarkhivirujte FunPayAPI.zip v papku backend\FunPayAPI
    pause
    exit /b 1
)
echo  [OK] FunPayAPI (lokal'naya versiya)

echo  [3/4] Proverka Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [OSHIBKA] Node.js ne najden!
    echo  Ustanovite Node.js s https://nodejs.org/
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do set NODE_VER=%%v
echo  [OK] Node.js %NODE_VER%

echo  [4/4] Proverka npm-zavisimostej...
if not exist "%ROOT%node_modules\electron" (
    echo.
    echo  Ustanavlivayu Electron...
    cd /d "%ROOT%"
    npm install
    if errorlevel 1 (
        echo.
        echo  [OSHIBKA] npm install zavershilsya s oshibkoj!
        pause
        exit /b 1
    )
)
echo  [OK] npm-zavisimosti gotovy

echo.
echo  Zapuskayu prilozhenie...
echo.
cd /d "%ROOT%"
npm start

if errorlevel 1 (
    echo.
    echo  [OSHIBKA] Prilozhenie zavershilos s oshibkoj!
    echo  Proverjte logi: backend\logs\backend.log
)
echo.
pause
