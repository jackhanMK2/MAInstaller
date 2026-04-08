@echo off
chcp 65001 >nul
echo ============================================
echo   Mobile App Installer - Build
echo ============================================
echo.

python -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --console ^
    --name "MobileAppInstaller" ^
    --add-data "templates;templates" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import uvicorn.lifespan.off ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.http.httptools_impl ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.protocols.websockets.wsproto_impl ^
    --hidden-import uvicorn.protocols.websockets.websockets_impl ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.loops.asyncio ^
    --hidden-import multipart ^
    --hidden-import tidevice ^
    --hidden-import tidevice._usbmux ^
    --hidden-import tidevice._device ^
    --collect-submodules tidevice ^
    --hidden-import OpenSSL ^
    --collect-submodules OpenSSL ^
    --hidden-import pyasn1 ^
    --collect-submodules pyasn1 ^
    --hidden-import webview ^
    --collect-all webview ^
    --hidden-import clr_loader ^
    --collect-all clr_loader ^
    --hidden-import pythonnet ^
    --hidden-import clr ^
    --hidden-import bottle ^
    --hidden-import proxy_tools ^
    app.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo   빌드 완료!
    echo   dist\MobileAppInstaller.exe
    echo ============================================
) else (
    echo.
    echo   빌드 실패!
    pause
)
