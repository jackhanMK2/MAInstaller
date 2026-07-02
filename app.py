import sys
import os
import uuid
import json
import asyncio
from pathlib import Path
from dataclasses import asdict

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from device_manager import DeviceManager, InstallStatus, Platform, get_apk_package_name

app = FastAPI(title="Mobile App Installer")

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def _get_resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent

BASE_DIR = _get_base_dir()
RESOURCE_DIR = _get_resource_dir()
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(RESOURCE_DIR / "templates"))
dm = DeviceManager()

# ── WebSocket connections for live status updates ──
ws_clients: set[WebSocket] = set()


async def broadcast(msg: dict):
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect x="14" y="4" width="36" height="56" rx="6" fill="#1a1d27" stroke="#6c5ce7" stroke-width="3"/>
<rect x="22" y="8" width="20" height="2" rx="1" fill="#6c5ce7" opacity=".4"/>
<path d="M32 22v16m-7-5l7 7 7-7" stroke="#00b894" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
<rect x="26" y="48" width="12" height="3" rx="1.5" fill="#6c5ce7" opacity=".5"/>
</svg>'''


# ── Pages ──

@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── API: Tool status ──

@app.get("/api/tools")
async def tool_status():
    return dm.get_tool_status()


# ── API: Devices ──

@app.get("/api/devices")
async def list_devices():
    devices = dm.get_all_devices()
    return [asdict(d) for d in devices]


# ── API: Pull media (latest screenshot & recording → Downloads) ──

def _downloads_dir() -> Path:
    """사용자 다운로드 폴더 경로 (Windows는 알려진 폴더 레지스트리 우선)"""
    if sys.platform == "win32":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
        except Exception:
            pass
    p = Path.home() / "Downloads"
    return p if p.is_dir() else Path.home()


@app.post("/api/pull-media")
async def pull_media(body: dict):
    device_id = body.get("device_id")
    devices = dm.get_all_devices()
    device = next((d for d in devices if d.id == device_id), None)
    if not device:
        return JSONResponse({"error": "디바이스를 찾을 수 없습니다."}, status_code=404)
    if device.status != "connected":
        return JSONResponse({"error": "연결된(승인된) 디바이스만 가져올 수 있습니다."}, status_code=400)

    dest = _downloads_dir()

    await broadcast({
        "type": "media_start",
        "device_id": device.id,
        "device_name": device.name,
    })

    loop = asyncio.get_running_loop()

    def _progress(msg: str):
        asyncio.run_coroutine_threadsafe(
            broadcast({
                "type": "media_progress",
                "device_id": device.id,
                "device_name": device.name,
                "message": msg,
            }),
            loop,
        )

    result = await asyncio.to_thread(dm.pull_media, device, str(dest), _progress)

    if result.get("ok"):
        await broadcast({
            "type": "media_done",
            "device_id": device.id,
            "device_name": device.name,
            "count": result.get("count", 0),
            "path": result.get("path", ""),
            "screenshot": result.get("screenshot"),
            "recording": result.get("recording"),
        })
        if result.get("count", 0) > 0 and sys.platform == "win32":
            try:
                os.startfile(result["path"])  # noqa: S606
            except Exception:
                pass
    else:
        await broadcast({
            "type": "media_error",
            "device_id": device.id,
            "device_name": device.name,
            "message": result.get("error", "알 수 없는 오류"),
        })

    return result


# ── API: Upload file ──

METADATA_PATH = UPLOAD_DIR / "metadata.json"
uploaded_files: dict[str, dict] = {}


def _load_metadata():
    if METADATA_PATH.exists():
        try:
            uploaded_files.update(json.loads(METADATA_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass


def _save_metadata():
    try:
        METADATA_PATH.write_text(json.dumps(uploaded_files, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_metadata()


@app.get("/api/files")
async def list_files():
    return list(uploaded_files.values())


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in (".apk", ".ipa"):
        return JSONResponse(
            {"error": "APK 또는 IPA 파일만 업로드할 수 있습니다."},
            status_code=400,
        )

    file_id = uuid.uuid4().hex[:12]
    save_name = f"{file_id}{ext}"
    save_path = UPLOAD_DIR / save_name

    content = await file.read()
    save_path.write_bytes(content)

    size_mb = len(content) / (1024 * 1024)
    package_name = ""
    if ext == ".apk":
        package_name = get_apk_package_name(str(save_path)) or ""

    info = {
        "id": file_id,
        "name": file.filename,
        "path": str(save_path),
        "ext": ext,
        "platform": "android" if ext == ".apk" else "ios",
        "size": f"{size_mb:.1f} MB",
        "package": package_name,
    }
    uploaded_files[file_id] = info
    _save_metadata()
    return info


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: str):
    info = uploaded_files.pop(file_id, None)
    if not info:
        return JSONResponse({"error": "파일을 찾을 수 없습니다."}, status_code=404)
    try:
        Path(info["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    _save_metadata()
    return {"ok": True}


# ── API: Install ──

@app.post("/api/install")
async def install(body: dict):
    file_id = body.get("file_id")
    device_ids = body.get("device_ids", [])
    install_all = body.get("install_all", False)
    clean_install = body.get("clean_install", False)

    info = uploaded_files.get(file_id)
    if not info:
        return JSONResponse({"error": "파일을 찾을 수 없습니다."}, status_code=404)

    devices = dm.get_all_devices()
    if install_all:
        targets = devices
    else:
        id_set = set(device_ids)
        targets = [d for d in devices if d.id in id_set]

    if not targets:
        return JSONResponse({"error": "설치 대상 디바이스가 없습니다."}, status_code=400)

    mode_label = "삭제 후 재설치" if clean_install else "덮어쓰기 설치"
    await broadcast({
        "type": "install_start",
        "file": info["name"],
        "device_count": len(targets),
        "mode": mode_label,
    })

    for d in targets:
        msg = "기존 앱 삭제 후 설치 중..." if clean_install else "설치 중..."
        await broadcast({
            "type": "install_status",
            "device_id": d.id,
            "device_name": d.name,
            "status": "installing",
            "message": msg,
        })

    results = await dm.install_to_multiple(targets, info["path"], clean=clean_install)

    out = []
    for r in results:
        device_name = next((d.name for d in targets if d.id == r.device_id), r.device_id)
        entry = {
            "device_id": r.device_id,
            "device_name": device_name,
            "status": r.status.value,
            "message": r.message,
        }
        out.append(entry)
        await broadcast({"type": "install_status", **entry})

    success = sum(1 for r in results if r.status == InstallStatus.SUCCESS)
    await broadcast({
        "type": "install_done",
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
    })

    return out


# ── WebSocket ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


# ── Entrypoint ──

def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(host: str, port: int):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _hide_console():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0
            )
        except Exception:
            pass


def _show_error_msgbox(title: str, msg: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:
        pass


def _find_browser():
    import shutil
    candidates = [
        (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"), "Chrome"),
        (os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"), "Chrome"),
        (os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"), "Chrome"),
    ]
    for path, name in candidates:
        if os.path.isfile(path):
            return path
    for exe in ("msedge", "chrome"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def _open_as_app_window(url: str):
    """pywebview 실패 시 브라우저 앱 모드로 대체"""
    import subprocess
    browser_path = _find_browser()
    if browser_path:
        user_data = str(BASE_DIR / ".app_profile")
        proc = subprocess.Popen([
            browser_path, f"--app={url}",
            f"--user-data-dir={user_data}",
            "--window-size=1200,820",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        return proc
    else:
        import webbrowser
        webbrowser.open(url)
        return None


def _wait_for_server(url: str, retries: int = 50):
    import time
    import urllib.request
    for _ in range(retries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


if __name__ == "__main__":
    try:
        import threading

        PORT = _find_free_port()
        HOST = "127.0.0.1"
        URL = f"http://{HOST}:{PORT}"

        server_thread = threading.Thread(target=_run_server, args=(HOST, PORT), daemon=True)
        server_thread.start()
        _wait_for_server(URL)

        # pywebview 시도 → 실패 시 브라우저 앱 모드로 대체
        try:
            import webview
            window = webview.create_window(
                "Mobile App Installer",
                URL,
                width=1200,
                height=820,
                min_size=(900, 600),
            )

            def _on_shown():
                if getattr(sys, "frozen", False):
                    _hide_console()

            webview.start(func=_on_shown)

        except Exception:
            if getattr(sys, "frozen", False):
                _hide_console()
            proc = _open_as_app_window(URL)
            if proc:
                proc.wait()
            else:
                print("=" * 50)
                print("  Mobile App Installer")
                print(f"  {URL}")
                print("  종료: Ctrl+C")
                print("=" * 50)
                try:
                    server_thread.join()
                except KeyboardInterrupt:
                    pass

    except Exception as e:
        msg = f"앱 실행 중 오류가 발생했습니다.\n\n{type(e).__name__}: {e}"
        print(msg)
        _show_error_msgbox("Mobile App Installer - 오류", msg)
        if getattr(sys, "frozen", False):
            input("Enter를 눌러 종료...")
