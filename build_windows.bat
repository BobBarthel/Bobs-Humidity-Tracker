@echo off
setlocal

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if "%PY%"=="" (
  where python3.12 >nul 2>nul
  if not errorlevel 1 set "PY=python3.12"
)
if "%PY%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
  echo Python not found. Install Python 3.12 and ensure it's on PATH. 1>&2
  exit /b 1
)
set "PKGFILE=%TEMP%\\bh_packages_%RANDOM%.txt"
if exist "%PKGFILE%" del "%PKGFILE%"
set "MISSING=0"

for %%M in (PyInstaller bleak webview PIL) do (
  %PY% -c "import sys;__import__('%%M')" 1>nul 2>nul
  if errorlevel 1 (
    echo Missing dependency: %%M 1>&2
    set "MISSING=1"
    if /I "%%M"=="webview" (
      echo pywebview>>"%PKGFILE%"
    ) else if /I "%%M"=="PIL" (
      echo pillow>>"%PKGFILE%"
    ) else (
      echo %%M>>"%PKGFILE%"
    )
  )
)

if "%MISSING%"=="1" (
  set "PACKAGES="
  if exist "%PKGFILE%" (
    for /f "usebackq delims=" %%P in ("%PKGFILE%") do set "PACKAGES=%PACKAGES% %%P"
    del "%PKGFILE%"
  )
  choice /M "Install missing dependencies now"
  if errorlevel 2 (
    echo Install missing dependencies and rerun. 1>&2
    exit /b 1
  )
  %PY% -m pip install %PACKAGES%
  if errorlevel 1 (
    echo Dependency install failed. 1>&2
    echo If this failed on pythonnet, use Python 3.12 or 3.11, or install Windows Build Tools and NuGet. 1>&2
    exit /b 1
  )
)

%PY% -m PyInstaller "Bobs Humidity Tracker Windows.spec"
