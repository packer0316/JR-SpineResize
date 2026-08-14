@echo off
chcp 65001 >nul
echo ========================================
echo   JR-SpineResize 打包工具
echo ========================================
echo.

cd /d "%~dp0"

set "RELEASE_DIR=%~dp0release"
set "BUILD_TEMP=%~dp0build_temp"
set "ICON_PATH=%~dp0ico\JR-SpineResize.ico"
set "APP_NAME=JR-SpineResize"

echo 清理舊的輸出與暫存...
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
if exist "%BUILD_TEMP%" rmdir /s /q "%BUILD_TEMP%"

echo 開始打包...
echo.

py -3 -m PyInstaller ^
  --onefile ^
  --windowed ^
  --clean ^
  --noconfirm ^
  --name "%APP_NAME%" ^
  --icon "%ICON_PATH%" ^
  --add-data "%~dp0ico\JR-SpineResize.ico;ico" ^
  --distpath "%RELEASE_DIR%" ^
  --workpath "%BUILD_TEMP%" ^
  --specpath "%BUILD_TEMP%" ^
  --hidden-import numpy --collect-submodules numpy ^
  --hidden-import imagequant --collect-all imagequant ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module tkinter ^
  main.py

set "BUILD_RESULT=%ERRORLEVEL%"

echo 清理打包暫存檔...
if exist "%BUILD_TEMP%" rmdir /s /q "%BUILD_TEMP%"

echo.
if %BUILD_RESULT% EQU 0 (
    echo ========================================
    echo   打包完成！
    echo   主程式: %RELEASE_DIR%\%APP_NAME%.exe
    echo ========================================
) else (
    echo ========================================
    echo   打包失敗！請檢查錯誤訊息。
    echo ========================================
)

echo.
pause
