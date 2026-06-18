import asyncio
import subprocess
import shutil
import struct
import zipfile
import plistlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class Platform(str, Enum):
    ANDROID = "android"
    IOS = "ios"


class InstallStatus(str, Enum):
    PENDING = "pending"
    INSTALLING = "installing"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class Device:
    id: str
    name: str
    platform: Platform
    model: str = ""
    status: str = "connected"
    storage_free: str = ""
    storage_total: str = ""


@dataclass
class InstallResult:
    device_id: str
    status: InstallStatus
    message: str = ""


# ── APK package name extraction (no aapt needed) ──

def get_apk_package_name(apk_path: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(apk_path) as z:
            data = z.read("AndroidManifest.xml")
    except Exception:
        return None
    return _parse_manifest_package(data)


def _parse_manifest_package(data: bytes) -> Optional[str]:
    if len(data) < 16:
        return None

    strings = _extract_string_pool(data)
    if not strings:
        return None

    pkg_idx = None
    for i, s in enumerate(strings):
        if s == "package":
            pkg_idx = i
            break

    if pkg_idx is None:
        return None

    pos = 8
    chunk_type = struct.unpack_from("<H", data, pos)[0]
    chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
    pos += chunk_size

    if pos < len(data) - 4:
        chunk_type = struct.unpack_from("<H", data, pos)[0]
        if chunk_type == 0x0180:
            chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
            pos += chunk_size

    while pos < len(data) - 8:
        chunk_type = struct.unpack_from("<H", data, pos)[0]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_size < 8:
            break

        if chunk_type == 0x0102:
            attr_count = struct.unpack_from("<H", data, pos + 28)[0]
            for i in range(attr_count):
                attr_pos = pos + 36 + i * 20
                if attr_pos + 20 > len(data):
                    break
                attr_name_idx = struct.unpack_from("<I", data, attr_pos + 4)[0]
                if attr_name_idx == pkg_idx:
                    raw_idx = struct.unpack_from("<I", data, attr_pos + 8)[0]
                    if raw_idx != 0xFFFFFFFF and raw_idx < len(strings):
                        return strings[raw_idx]
                    typed = struct.unpack_from("<I", data, attr_pos + 16)[0]
                    if typed < len(strings):
                        return strings[typed]
            break

        pos += chunk_size

    for s in strings:
        if "." in s and len(s) > 5 and not s.startswith(("http", "/", "android")):
            parts = s.split(".")
            if 2 <= len(parts) <= 6 and all(p.replace("_", "").isalnum() for p in parts if p):
                return s
    return None


def _extract_string_pool(data: bytes) -> list[str]:
    if len(data) < 28:
        return []

    pos = 8
    chunk_type = struct.unpack_from("<H", data, pos)[0]
    if chunk_type != 0x0001:
        return []

    string_count = struct.unpack_from("<I", data, pos + 8)[0]
    flags = struct.unpack_from("<I", data, pos + 16)[0]
    strings_start = struct.unpack_from("<I", data, pos + 20)[0]
    is_utf8 = bool(flags & (1 << 8))

    offsets = []
    for i in range(min(string_count, 10000)):
        offsets.append(struct.unpack_from("<I", data, pos + 28 + i * 4)[0])

    abs_start = pos + strings_start
    results = []
    for offset in offsets:
        try:
            sp = abs_start + offset
            if is_utf8:
                b = data[sp]
                sp += 2 if b & 0x80 else 1
                b = data[sp]
                if b & 0x80:
                    byte_len = ((b & 0x7F) << 8) | data[sp + 1]
                    sp += 2
                else:
                    byte_len = b
                    sp += 1
                results.append(data[sp : sp + byte_len].decode("utf-8", errors="replace"))
            else:
                str_len = struct.unpack_from("<H", data, sp)[0]
                sp += 2
                if str_len & 0x8000:
                    str_len = ((str_len & 0x7FFF) << 16) | struct.unpack_from("<H", data, sp)[0]
                    sp += 2
                results.append(data[sp : sp + str_len * 2].decode("utf-16-le", errors="replace"))
        except Exception:
            results.append("")
    return results


# ── IPA bundle ID extraction ──

def get_ipa_bundle_id(ipa_path: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(ipa_path) as z:
            for name in z.namelist():
                if name.endswith("Info.plist") and name.count("/") == 2:
                    data = z.read(name)
                    plist = plistlib.loads(data)
                    return plist.get("CFBundleIdentifier")
    except Exception:
        pass
    return None


# ── ADB error message translation ──

_ADB_ERROR_MAP = {
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "저장 공간 부족 — 디바이스의 불필요한 앱/파일을 삭제하세요",
    "INSTALL_FAILED_ALREADY_EXISTS": "동일 앱이 이미 설치됨 — '삭제 후 재설치'를 사용하세요",
    "INSTALL_FAILED_INVALID_APK": "APK 파일이 손상되었거나 유효하지 않습니다",
    "INSTALL_FAILED_INVALID_URI": "APK 파일 경로가 잘못되었습니다",
    "INSTALL_FAILED_NO_SHARED_USER": "공유 사용자 ID 불일치",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "기존 설치와 서명이 다름 — '삭제 후 재설치'를 사용하세요",
    "INSTALL_FAILED_SHARED_USER_INCOMPATIBLE": "공유 사용자 서명 불일치",
    "INSTALL_FAILED_MISSING_SHARED_LIBRARY": "필요한 공유 라이브러리가 디바이스에 없습니다",
    "INSTALL_FAILED_REPLACE_COULDNT_DELETE": "기존 앱 교체 실패 — '삭제 후 재설치'를 사용하세요",
    "INSTALL_FAILED_DEXOPT": "DEX 최적화 실패",
    "INSTALL_FAILED_OLDER_SDK": "디바이스 Android 버전이 너무 낮습니다",
    "INSTALL_FAILED_CONFLICTING_PROVIDER": "콘텐츠 프로바이더 충돌 — 충돌하는 앱을 먼저 삭제하세요",
    "INSTALL_FAILED_NEWER_SDK": "디바이스 Android 버전이 너무 높습니다",
    "INSTALL_FAILED_TEST_ONLY": "테스트 전용 APK — '-t' 옵션이 필요합니다",
    "INSTALL_FAILED_CPU_ABI_INCOMPATIBLE": "디바이스 CPU 아키텍처와 호환되지 않는 APK",
    "INSTALL_FAILED_MISSING_FEATURE": "디바이스에 필요한 하드웨어 기능이 없습니다",
    "INSTALL_FAILED_CONTAINER_ERROR": "컨테이너 오류",
    "INSTALL_FAILED_INVALID_INSTALL_LOCATION": "설치 위치가 유효하지 않습니다",
    "INSTALL_FAILED_MEDIA_UNAVAILABLE": "외부 저장소를 사용할 수 없습니다",
    "INSTALL_FAILED_VERIFICATION_TIMEOUT": "설치 검증 시간 초과",
    "INSTALL_FAILED_VERIFICATION_FAILURE": "설치 검증 실패",
    "INSTALL_FAILED_PACKAGE_CHANGED": "패키지 변경 감지됨",
    "INSTALL_FAILED_UID_CHANGED": "UID 변경됨",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "설치 버전이 기존보다 낮음 — '삭제 후 재설치'를 사용하세요",
    "INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE": "권한 모델 다운그레이드 불가",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "디바이스 CPU 아키텍처(ABI)와 호환되지 않는 APK",
    "INSTALL_FAILED_ABORTED": "사용자가 설치를 취소했습니다",
    "INSTALL_FAILED_SANDBOX_VERSION_DOWNGRADE": "샌드박스 버전 다운그레이드 불가",
    "INSTALL_PARSE_FAILED_NOT_APK": "파일이 APK 형식이 아닙니다",
    "INSTALL_PARSE_FAILED_BAD_MANIFEST": "AndroidManifest.xml 파싱 실패",
    "INSTALL_PARSE_FAILED_UNEXPECTED_EXCEPTION": "APK 파싱 중 예기치 않은 오류",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK에 서명이 없습니다",
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES": "APK 서명이 일치하지 않습니다",
}


def _translate_adb_error(output: str) -> str:
    for code, msg in _ADB_ERROR_MAP.items():
        if code in output:
            return f"{msg} [{code}]"
    if "Failure" in output:
        start = output.find("Failure")
        return output[start:start + 200].strip()
    return output.strip()[-500:]


# ── Device Manager ──

def _run_sync(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


class DeviceManager:
    def __init__(self):
        self._adb_path: Optional[str] = shutil.which("adb")
        self._tidevice_available = False
        try:
            import tidevice  # noqa: F401
            self._tidevice_available = True
        except ImportError:
            pass

    @property
    def adb_available(self) -> bool:
        return self._adb_path is not None

    @property
    def ios_available(self) -> bool:
        return self._tidevice_available

    def get_tool_status(self) -> dict:
        return {
            "adb": self._adb_path or "not found",
            "tidevice": "installed" if self._tidevice_available else "not found",
        }

    # ── Android devices ──

    def get_android_devices(self) -> list[Device]:
        if not self._adb_path:
            return []
        try:
            result = _run_sync([self._adb_path, "devices", "-l"])
            if result.returncode != 0:
                return []
            devices = []
            for line in result.stdout.strip().splitlines()[1:]:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                device_id = parts[0]
                adb_status = parts[1]

                VALID = {"device", "unauthorized", "authorizing", "recovery", "sideload"}
                if adb_status not in VALID:
                    continue

                STATUS_MAP = {
                    "device": "connected",
                    "unauthorized": "unauthorized",
                    "authorizing": "authorizing",
                    "recovery": "recovery",
                    "sideload": "sideload",
                }

                model = ""
                name = device_id
                for part in parts[2:]:
                    if part.startswith("model:"):
                        model = part.split(":", 1)[1]
                        name = model
                    elif part.startswith("device:") and not model:
                        name = part.split(":", 1)[1]

                if adb_status == "unauthorized":
                    name = f"{name} (승인 필요)"

                storage_free, storage_total = "", ""
                if adb_status == "device":
                    storage_free, storage_total = self._get_android_storage(device_id)

                devices.append(Device(
                    id=device_id, name=name,
                    platform=Platform.ANDROID, model=model,
                    status=STATUS_MAP.get(adb_status, adb_status),
                    storage_free=storage_free, storage_total=storage_total,
                ))
            return devices
        except Exception:
            return []

    def _get_android_storage(self, device_id: str) -> tuple[str, str]:
        try:
            result = _run_sync([self._adb_path, "-s", device_id, "shell", "df", "/data"], timeout=5)
            for line in result.stdout.strip().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4:
                    total_kb = int(cols[1])
                    avail_kb = int(cols[3])
                    return self._fmt_size(avail_kb), self._fmt_size(total_kb)
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _fmt_size(kb: int) -> str:
        if kb >= 1048576:
            return f"{kb / 1048576:.1f} GB"
        return f"{kb / 1024:.0f} MB"

    # ── iOS devices (via tidevice Python API) ──

    def get_ios_devices(self) -> list[Device]:
        if not self._tidevice_available:
            return []
        try:
            from tidevice._usbmux import Usbmux
            from tidevice._device import BaseDevice
            um = Usbmux()
            devices = []
            for info in um.device_list():
                udid = info.udid
                name = udid
                model = ""
                storage_free, storage_total = "", ""
                try:
                    dev = BaseDevice(udid, um)
                    dev_info = dev.device_info()
                    name = dev_info.get("DeviceName", udid)
                    product_type = dev_info.get("ProductType", "")
                    device_class = dev_info.get("DeviceClass", "")
                    model = f"{device_class} ({product_type})" if product_type else device_class
                except Exception:
                    pass
                try:
                    disk = dev.get_io_power()
                    if disk and "DiskUsage" in disk:
                        total = disk["DiskUsage"].get("TotalDataCapacity", 0)
                        avail = disk["DiskUsage"].get("TotalDataAvailable", 0)
                        if total:
                            storage_total = f"{total / (1024**3):.1f} GB"
                        if avail:
                            storage_free = f"{avail / (1024**3):.1f} GB"
                except Exception:
                    pass
                devices.append(Device(
                    id=udid, name=name,
                    platform=Platform.IOS, model=model,
                    storage_free=storage_free, storage_total=storage_total,
                ))
            return devices
        except Exception:
            return []

    def get_all_devices(self) -> list[Device]:
        return self.get_android_devices() + self.get_ios_devices()

    # ── Android: get package name from installed APK ──

    def _get_installed_package(self, device_id: str, package_name: str) -> bool:
        if not self._adb_path or not package_name:
            return False
        try:
            result = _run_sync([
                self._adb_path, "-s", device_id,
                "shell", "pm", "list", "packages", package_name,
            ])
            return f"package:{package_name}" in result.stdout
        except Exception:
            return False

    def _uninstall_android(self, device_id: str, package_name: str) -> tuple[bool, str]:
        if not self._adb_path or not package_name:
            return False, "패키지명 없음"
        try:
            result = _run_sync(
                [self._adb_path, "-s", device_id, "uninstall", package_name],
                timeout=60,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0 and "Success" in output:
                return True, "삭제 완료"
            return False, output.strip()[-300:]
        except Exception as e:
            return False, str(e)

    def _install_android_sync(self, device_id: str, apk_path: str, clean: bool = False) -> InstallResult:
        if not self._adb_path:
            return InstallResult(device_id, InstallStatus.FAILED, "adb를 찾을 수 없습니다")

        package_name = get_apk_package_name(apk_path)
        file_size = Path(apk_path).stat().st_size
        timeout = max(300, int(file_size / (1024 * 1024)) * 3)

        if clean and package_name and self._get_installed_package(device_id, package_name):
            self._uninstall_android(device_id, package_name)

        try:
            result = _run_sync(
                [self._adb_path, "-s", device_id, "install", "--streaming", "-r", "-d", apk_path],
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0 and "Success" in output:
                mode = "삭제 후 설치 완료" if clean else "설치 완료"
                return InstallResult(device_id, InstallStatus.SUCCESS,
                                     f"{mode}{' (' + package_name + ')' if package_name else ''}")
            return InstallResult(device_id, InstallStatus.FAILED, _translate_adb_error(output))
        except subprocess.TimeoutExpired:
            return InstallResult(device_id, InstallStatus.FAILED, f"설치 시간 초과 ({timeout // 60}분)")
        except Exception as e:
            return InstallResult(device_id, InstallStatus.FAILED, str(e))

    # ── iOS uninstall + install ──

    def _ios_bundle_id(self, ipa_path: str) -> Optional[str]:
        """IPA 번들 ID 추출: 자체 파서 → tidevice IPAReader 폴백"""
        bundle_id = get_ipa_bundle_id(ipa_path)
        if bundle_id:
            return bundle_id
        try:
            from tidevice._ipautil import IPAReader
            ir = IPAReader(ipa_path)
            bundle_id = ir.get_bundle_id()
            ir.close()
            return bundle_id
        except Exception:
            return None

    def _uninstall_ios(self, dev, bundle_id: str) -> tuple[bool, str]:
        """앱이 설치되어 있으면 삭제하고 제거를 검증"""
        try:
            installed_before = dev.installation.lookup(bundle_id) is not None
            if not installed_before:
                return True, "기존 설치 없음"
            ok = dev.installation.uninstall(bundle_id)
            still_installed = dev.installation.lookup(bundle_id) is not None
            if still_installed:
                return False, "삭제 실패 (앱이 여전히 설치되어 있음)"
            return True, "삭제 완료"
        except Exception as e:
            return False, str(e)

    def _install_ios_sync(self, device_id: str, ipa_path: str, clean: bool = False) -> InstallResult:
        if not self._tidevice_available:
            return InstallResult(device_id, InstallStatus.FAILED,
                                 "tidevice가 설치되지 않았습니다 (pip install tidevice)")

        bundle_id = self._ios_bundle_id(ipa_path)

        try:
            from tidevice._usbmux import Usbmux
            from tidevice._device import BaseDevice
            um = Usbmux()
            dev = BaseDevice(device_id, um)

            uninstall_note = ""
            if clean:
                if not bundle_id:
                    return InstallResult(
                        device_id, InstallStatus.FAILED,
                        "삭제 후 재설치 실패: IPA에서 번들 ID를 추출할 수 없습니다",
                    )
                ok, note = self._uninstall_ios(dev, bundle_id)
                if not ok:
                    return InstallResult(
                        device_id, InstallStatus.FAILED,
                        f"기존 앱 삭제 실패로 중단: {note}",
                    )
                uninstall_note = note

            dev.app_install(ipa_path)

            if clean:
                detail = f" ({bundle_id})" if bundle_id else ""
                return InstallResult(device_id, InstallStatus.SUCCESS,
                                     f"삭제 후 설치 완료{detail} — {uninstall_note}")
            detail = f" ({bundle_id})" if bundle_id else ""
            return InstallResult(device_id, InstallStatus.SUCCESS, f"설치 완료{detail}")
        except Exception as e:
            return InstallResult(device_id, InstallStatus.FAILED, str(e))

    # ── Unified install ──

    async def install_to_device(self, device: Device, file_path: str, clean: bool = False) -> InstallResult:
        ext = Path(file_path).suffix.lower()
        if device.platform == Platform.ANDROID and ext == ".apk":
            return await asyncio.to_thread(self._install_android_sync, device.id, file_path, clean)
        elif device.platform == Platform.IOS and ext == ".ipa":
            return await asyncio.to_thread(self._install_ios_sync, device.id, file_path, clean)
        else:
            return InstallResult(
                device.id, InstallStatus.FAILED,
                f"호환되지 않는 조합: {device.platform.value} 디바이스에 {ext} 파일",
            )

    async def install_to_multiple(self, devices: list[Device], file_path: str, clean: bool = False) -> list[InstallResult]:
        tasks = [self.install_to_device(d, file_path, clean) for d in devices]
        return await asyncio.gather(*tasks)
