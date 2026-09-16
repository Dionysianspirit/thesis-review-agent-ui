@echo off
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_gui.ps1" %*
if errorlevel 1 (
  echo.
  echo 启动失败。请把上面的报错发给开发者。
  pause
)
