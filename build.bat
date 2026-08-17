@echo off
chcp 65001 >nul
setlocal
echo ========================================
echo   JR-SpineResize 打包工具
echo ========================================
echo.

cd /d "%~dp0"

set "RELEASE_DIR=%~dp0release"
set "BUILD_ROOT=%~dp0build_temp"
rem 每次執行用獨立的暫存子資料夾：兩個打包同時跑時若共用同一個 build_temp，
rem 先跑完那個的清理會把另一個的工作目錄刪掉，PyInstaller 半路 FileNotFoundError
set "BUILD_TEMP=%BUILD_ROOT%\run_%RANDOM%%RANDOM%"
set "ICON_PATH=%~dp0ico\JR-SpineResize.ico"
set "APP_NAME=JR-SpineResize"

if exist "%RELEASE_DIR%\%APP_NAME%.exe" (
    echo 清理舊的輸出...
    del /f /q "%RELEASE_DIR%\%APP_NAME%.exe" >nul 2>&1
)
if exist "%RELEASE_DIR%\%APP_NAME%.exe" (
    echo [錯誤] %APP_NAME%.exe 正在使用中，請先關閉程式再打包。
    goto :failed
)

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
  --hidden-import oxipng --collect-all oxipng ^
  --hidden-import mozjpeg_lossless_optimization --collect-all mozjpeg_lossless_optimization ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module tkinter ^
  main.py

set "BUILD_RESULT=%ERRORLEVEL%"

echo 清理打包暫存檔...
if exist "%BUILD_TEMP%" rmdir /s /q "%BUILD_TEMP%"
rem 沒有其他打包在跑時順手移除空的 build_temp 根目錄（非空就留著，不硬刪）
if exist "%BUILD_ROOT%" rmdir "%BUILD_ROOT%" 2>nul

echo.
if %BUILD_RESULT% NEQ 0 goto :failed
echo ========================================
echo   打包完成！
echo   主程式: %RELEASE_DIR%\%APP_NAME%.exe
echo ========================================
echo.
pause
exit /b 0

:failed
echo ========================================
echo   打包失敗！請檢查上方錯誤訊息。
echo   提醒：不要同時執行兩個 build.bat；
echo   若反覆失敗，檢查防毒軟體是否攔截 build_temp 或 release。
echo ========================================
echo.
pause
exit /b 1
