[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

브라우저에서 로컬 libmpv 플레이어를 제어합니다. 미디어 탐색, 대기열 관리, 트랙 선택, 탐색 이동 및 오디오·비디오 출력 선택을 제공합니다. 재생은 서비스를 실행하는 Linux 컴퓨터에서 이루어집니다.

## 요구 사항

Linux, Python 3.11 이상 및 동적 로더에서 찾을 수 있는 호환 시스템 libmpv가 필요합니다. 브라우저 인터페이스를 빌드하려면 Node.js 22.12 이상과 npm도 필요합니다. Redis는 선택 사항이며 사용할 때 `MPV_WEBD_REDIS_URL`을 설정합니다.

## 설치

소스를 체크아웃한 디렉터리에서 가상 환경을 만들고 브라우저 자산을 빌드한 다음 Python 패키지를 설치합니다.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
cd frontend
npm ci
npm run build
cd ..
python -m pip install .
```

준비된 wheel을 설치하려면 Python 가상 환경을 활성화한 후 wheel 파일이 있는 디렉터리에서 실행합니다.

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## 설정 및 시작

새로 설치할 때는 기존 미디어 디렉터리와 쓰기 가능한 절대 데이터베이스 경로를 선택합니다. `$HOME/Videos`를 자신의 미디어 디렉터리로 바꾸세요. 아래 환경 변수는 이후 명령과 같은 셸에서 설정합니다.

업그레이드 전에 데이터베이스를 백업하고 계속 사용할 데이터베이스를 `MPV_WEBD_RUNTIME_DB`로 지정하세요. 다른 경로를 선택하면 별도의 상태가 생성됩니다. 백업하거나 옮길 때 SQLite 보조 파일도 함께 보관하세요.

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

설치한 패키지 API로 브라우저 액세스 토큰을 만듭니다. 명령이 토큰을 한 번 표시합니다. 안전하게 보관하고 연결 양식에 붙여 넣으세요. URL, 명령 인수, 스크린샷 또는 공유 로그에 넣지 마세요.

```sh
python - <<'PYTOKEN'
from backend.config.sources import load_configuration
from backend.db.migration_runner import MigrationRunner
from backend.db.runtime_path import runtime_database_path
from backend.auth import TokenStore

load_configuration()
runner = MigrationRunner(runtime_database_path())
runner.apply()
issued = TokenStore(runner.connect).issue("browser")
print(issued.token)
PYTOKEN
```

서비스를 시작한 후 <http://127.0.0.1:8000>을 엽니다.

```sh
mpv-webd
```

## 사용 방법

인터페이스에서 영어 또는 중국어 간체를 선택하고 토큰을 입력해 연결합니다. Library에서 허용된 폴더를 열고 파일을 선택하여 Queue에 추가하거나 Play now를 누릅니다. Info에는 재생과 트랙 제어가 있고 Settings에서는 출력 장치를 선택한 뒤 Apply를 누릅니다. 추가 결과가 확인되지 않았으면 중복을 피하도록 대기열을 새로 고친 후 다시 시도하세요. 포그라운드 서비스는 Ctrl+C로 중지합니다.

## 이식 가능한 설정

프로세스 환경 변수가 설정 파일보다 우선합니다. 기본 파일은 `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env`이며 서비스 계정 소유의 일반 파일이어야 하고 권한은 `0600`이어야 합니다. `NAME=VALUE` 형식을 사용하고 공백이 있는 값은 따옴표로 감쌉니다. 자격 증명은 이 파일에 저장하지 마세요. `XDG_STATE_HOME`을 설정할 때는 절대 경로를 사용합니다. 명시적 경로가 없으면 새 상태는 `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite`에 저장됩니다. `/var/lib/mpv-webd/runtime.sqlite`에 기존 데이터가 있거나 접근할 수 없으면 데이터베이스를 명시적으로 선택해야 합니다.

미디어 루트는 절대 경로로 지정하고 Linux에서는 `:`로 구분합니다. `/home` 또는 `/var/home` 아래에 설정된 홈 디렉터리에 대해 `/media/<account>`와 `/run/media/<account>`의 바로 아래 실제 마운트 지점을 찾습니다. `MPV_WEBD_MEDIA_MOUNT_PARENT`로 부모 디렉터리를 지정하거나 `MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0`으로 검색을 끌 수 있습니다. 장치 연결이 끊기면 루트를 다시 선택하세요.

로컬 사용 시 루프백에서 수신하세요. 네트워크 수신 시 인증을 켜고 정확한 브라우저 Origin을 허용하세요. 토큰을 안전하게 보관하고 신뢰할 수 없는 네트워크에서는 암호화된 전송을 사용하세요. 개발용 인증 우회 설정을 네트워크에서 활성화하지 마세요.

## 네이티브 렌더러

선택적 렌더러는 [업스트림 안내](../vendor/Omniphony/omniphony-renderer/README.md)에 따라 빌드하고 리소스와 라이선스 고지를 유지합니다. [패치 출처](../UPSTREAM_PATCHES.md)에는 포함된 유한 입력 수정 두 개가 설명되어 있습니다. GPU-next/DRM 기능을 검증하려면 보호된 정책 파일에 `schema_version`을 `1`로 지정하고 정확한 소문자 SHA-256 값인 `native_library_sha256`과 `libplacebo_sha256`을 넣습니다. 기본 경로는 `/etc/mpv-web-suite/native-library-identities.json`이며 `MPV_WEBD_NATIVE_IDENTITY_POLICY`로 다른 절대 경로를 선택합니다. 파일과 부모 디렉터리는 root 또는 서비스 계정 소유이며 그룹과 다른 사용자에게 쓰기 권한이 없어야 합니다.

## 릴리스 빌드

태그가 지정된 깨끗한 체크아웃에서 build를 설치하고 릴리스 스크립트를 실행합니다. 정확한 태그 아카이브에서 wheel과 소스 배포본을 만들고 커밋 서명을 검증하며 소스, 출처 및 체크섬을 Git이 무시하는 `build/release/`에 저장합니다. 출력 디렉터리는 비어 있어야 합니다.

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

출력 디렉터리에서 `sha256sum -c SHA256SUMS`를 실행하세요. 서명된 커밋은 `git verify-commit v0.0.1^{commit}`로 확인합니다. 주석 태그는 `v0.0.1`이고 제목은 **Fire up**입니다.

## 라이선스

애플리케이션 자체 코드는 **GPL-3.0-only**입니다. [LICENSE](../LICENSE)를 참조하세요. 구성 요소의 기존 라이선스와 저작권은 유지됩니다. Omniphony는 GPL-3.0-or-later, 참조 브리지는 GPL-3.0-only, SAF/KEMAR 리소스는 ISC, Vue는 MIT입니다. [타사 고지](../THIRD_PARTY_NOTICES.md)와 각 구성 요소의 원래 고지를 확인하세요.
