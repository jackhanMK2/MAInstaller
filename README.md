# Mobile App Installer

APK/IPA 파일을 연결된 여러 모바일 디바이스에 동시에 설치하는 도구입니다.

## 기능

- **파일 등록**: APK (Android) / IPA (iOS) 파일 드래그앤드롭 업로드
- **디바이스 감지**: USB 연결된 Android/iOS 디바이스 자동 감지
- **선택 설치**: 특정 디바이스를 선택하여 설치
- **전체 설치**: 연결된 모든 디바이스에 동시 설치
- **삭제 후 재설치**: 토글로 기존 앱 삭제 후 클린 설치 선택 가능
- **실시간 로그**: WebSocket을 통한 설치 진행 상태 실시간 표시

---

## 배포판 사용법 (exe)

### 사전 준비

| 대상 | 필요 도구 | 설치 방법 |
|------|-----------|-----------|
| Android | ADB (Android Debug Bridge) | [platform-tools](https://developer.android.com/tools/releases/platform-tools) 다운로드 후 PATH 등록 |
| iOS | iTunes 또는 Apple Mobile Device Driver | [iTunes](https://www.apple.com/kr/itunes/) 설치 |

> **Android 디바이스**: 설정 → 개발자 옵션 → USB 디버깅 활성화 필수

### 실행

1. `MobileAppInstaller.exe` 실행
2. 브라우저가 자동으로 `http://localhost:8080` 열림
3. APK/IPA 파일 드래그앤드롭으로 등록
4. "새로고침" 버튼으로 디바이스 검색
5. 파일 선택 → 설치 버튼 클릭

종료: 콘솔 창에서 `Ctrl+C` 또는 창 닫기

---

## 개발 환경 설정

### 요구사항

- Python 3.10+
- ADB (Android)
- iTunes (iOS, Windows)

### 설치 및 실행

```bash
pip install -r requirements.txt
python app.py
```

브라우저에서 `http://localhost:8080` 접속

### exe 빌드

```bash
pip install pyinstaller
build.bat
```

결과물: `dist/MobileAppInstaller.exe`
