[English](README.md) | [Français](docs/README.fr.md) | [简体中文](docs/README.zh-CN.md) | [日本語](docs/README.ja.md) | [한국어](docs/README.ko.md) | [Español](docs/README.es.md) | [Português](docs/README.pt.md)

# mpv web suite

A browser remote for a local libmpv player. Browse media, manage the queue, select tracks, seek, and choose audio/video outputs from your browser. Playback runs on the Linux computer hosting the service.

## Requirements

Linux, Python 3.11 or newer, and a compatible system libmpv available to the dynamic loader. Building the browser interface also requires Node.js 22.12 or newer and npm. Redis is optional; configure `MPV_WEBD_REDIS_URL` when using it.

## Install

From a source checkout, create a virtual environment, build the browser assets, and install the Python package:

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

To install the prepared wheel instead, activate a Python virtual environment and run this command in the directory containing the wheel:

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## Configure and start

For a new installation, choose an existing media directory and an absolute writable database path. Replace `$HOME/Videos` with your media directory. Run these exports in the same shell used for the commands below:

When upgrading, back up your database first and point `MPV_WEBD_RUNTIME_DB` at the database you intend to keep using. Choosing a different path creates separate state. Keep SQLite sidecars with the database during backup or relocation.

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

Create a browser access token with the installed package API. The command displays the token once; store it privately and paste it into the connection form. Do not place it in URLs, shell arguments, screenshots, or shared logs:

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

Start the service, then open <http://127.0.0.1:8000>:

```sh
mpv-webd
```

## Use

Choose English or Simplified Chinese in the interface, enter the token, and connect. Library opens your allowed media folders; select files and add them to Queue or choose Play now. Info contains playback and track controls. Settings selects outputs; choose a device and press Apply. Refresh the queue before retrying an unconfirmed addition to avoid duplicates. Stop the foreground service with Ctrl+C.

## Portable configuration

Process environment values override the configuration file. The default file is `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env`; it must be a regular file owned by the service account with mode `0600`. Use `NAME=VALUE` lines and quote whitespace. Keep credentials out of this file. `XDG_STATE_HOME` must be absolute when set. Without an explicit database path, new state defaults to `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite`; existing or inaccessible legacy state at `/var/lib/mpv-webd/runtime.sqlite` requires an explicit database choice.

Media roots use absolute paths separated by `:` on Linux. For configured homes under `/home` or `/var/home`, discovery checks actual immediate mountpoints under `/media/<account>` and `/run/media/<account>`. Set `MPV_WEBD_MEDIA_MOUNT_PARENT` to one explicit parent or `MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` to disable discovery. Choose a root again if its device has been disconnected.

Keep the listener on loopback for local use. For a network listener, enable authentication and allow the exact browser Origin. Keep tokens private and use encrypted transport on untrusted networks. Never enable the unsafe development authentication override on a network.

## Native renderer

Build the optional renderer by following its [upstream instructions](vendor/Omniphony/omniphony-renderer/README.md); retain its resources and component notices. [Patch provenance](UPSTREAM_PATCHES.md) identifies both included finite-input patches. To configure GPU-next/DRM native capability verification, use a protected policy file with schema version `1` and exact lowercase SHA-256 values for `native_library_sha256` and `libplacebo_sha256`. The default path is `/etc/mpv-web-suite/native-library-identities.json`; `MPV_WEBD_NATIVE_IDENTITY_POLICY` selects another absolute path. The file and parent directories must be owned by root or the service account and not writable by group or others.

## Build a release

From the clean tagged checkout, install the build frontend and run the release helper. It builds wheel and source distribution from the exact tagged archive, verifies the commit signature, and writes source, provenance, and checksums to the ignored `build/release/` directory. Use an empty output directory:

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

In the output directory, verify the files with `sha256sum -c SHA256SUMS`. Inspect the signed commit with `git verify-commit v0.0.1^{commit}`. The annotated tag is `v0.0.1`, titled **Fire up**.

## License

Application-owned code is **GPL-3.0-only**; see [LICENSE](LICENSE). Included components retain their licenses and copyright: Omniphony GPL-3.0-or-later, its reference bridge GPL-3.0-only, the SAF/KEMAR resource ISC, and Vue MIT. See [third-party notices](THIRD_PARTY_NOTICES.md) and the original component notices.
