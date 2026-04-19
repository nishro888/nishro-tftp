@echo off
REM Build Nishro TFTP as a Windows one-folder .exe distribution.
REM Output: dist\nishro_tftp\nishro_tftp.exe
REM
REM Run from the v1\ directory. Requires:
REM   - pip install -r requirements.txt
REM   - pip install pyinstaller
REM   - MinGW-w64 (mingw32-make, gcc) -- for the native C engine
REM   - Npcap SDK at C:\Npcap-SDK (override with NPCAP_SDK=...)

setlocal
cd /d "%~dp0"

REM Use the same Python that has the project deps installed.
REM (PyInstaller must run under the interpreter where yaml/scapy/etc live,
REM otherwise it can't find them during analysis.)
set PY="C:\Program Files\Python313\python.exe"
if not exist %PY% set PY=python

%PY% -c "import yaml, scapy, fastapi, aioftp, psutil" 2>nul
if errorlevel 1 (
    echo ERROR: project dependencies not installed in %PY%
    echo Run:  %PY% -m pip install -r requirements.txt
    goto :err
)

%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    %PY% -m pip install pyinstaller || goto :err
)

echo.
echo [1/3] Cleaning old build artifacts...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [2/3] Building native C TFTP engine (c_core\)...
where mingw32-make >nul 2>nul
if errorlevel 1 (
    echo WARNING: mingw32-make not on PATH; skipping C engine rebuild.
    echo          The bundled exe will ship whatever c_core\bin\nishro_core.exe
    echo          currently exists. Install MSYS2 mingw-w64 and re-run to refresh.
) else (
    pushd c_core
    mingw32-make
    if errorlevel 1 (
        popd
        goto :err
    )
    popd
)

echo.
echo [3/3] Running PyInstaller...
%PY% -m PyInstaller nishro_tftp.spec --clean --noconfirm || goto :err

echo.
echo =========================================================
echo  BUILD OK
echo  Output:  dist\nishro_tftp\nishro_tftp.exe
echo.
echo  To deploy:
echo    1. Copy the whole dist\nishro_tftp\ folder to the target machine
echo    2. Ensure Npcap is installed on the target (npcap.com)
echo    3. Right-click nishro_tftp.exe -> Run as administrator
echo =========================================================
exit /b 0

:err
echo.
echo BUILD FAILED
exit /b 1
