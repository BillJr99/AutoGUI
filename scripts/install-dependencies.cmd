@echo off
REM Convenience shim — runs install-dependencies.ps1 with a relaxed
REM execution policy so you don't have to type the full PowerShell
REM command manually.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-dependencies.ps1" %*
exit /b %ERRORLEVEL%
