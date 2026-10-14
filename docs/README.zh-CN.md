[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

通过浏览器遥控本地 libmpv 播放器。浏览媒体、管理队列、选择轨道、跳转播放位置并选择音视频输出。播放由运行服务的 Linux 计算机完成。

## 运行要求

Linux、Python 3.11 或更高版本，以及动态加载器可找到的兼容系统 libmpv。构建浏览器界面还需要 Node.js 22.12 或更高版本和 npm。Redis 可选，使用时设置 `MPV_WEBD_REDIS_URL`。

## 安装

在源码检出目录中创建虚拟环境，构建浏览器资源并安装 Python 包：

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

如使用已准备的 wheel，先激活 Python 虚拟环境，再在 wheel 所在目录执行：

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## 配置与启动

新安装请选择已存在的媒体目录，以及可写的绝对数据库路径。将 `$HOME/Videos` 替换为自己的媒体目录。以下环境变量与后续命令在同一终端设置：

升级前请备份数据库，并将 `MPV_WEBD_RUNTIME_DB` 指向要继续使用的数据库。选择其他路径会创建独立状态。备份或迁移时请一并保留 SQLite 附属文件。

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

通过已安装包的 API 创建浏览器访问令牌。命令显示一次令牌，请妥善保存并粘贴到连接表单。不要将令牌放入网址、命令参数、截图或共享日志：

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

启动服务，然后打开 <http://127.0.0.1:8000>：

```sh
mpv-webd
```

## 使用

在界面选择英文或简体中文，输入令牌后连接。在“媒体库”打开允许访问的目录，选中文件加入“队列”或点击“立即播放”。“信息”包含播放与轨道控件；“设置”中选好输出设备后点击应用。加入队列的结果未确认时，先刷新队列再重试，避免重复添加。前台服务使用 Ctrl+C 停止。

## 可移植配置

进程环境变量优先于配置文件。默认文件为 `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env`，须为服务账户所有的普通文件，权限为 `0600`。使用 `NAME=VALUE` 格式，含空白的值加引号，不要存放凭据。设置 `XDG_STATE_HOME` 时必须使用绝对路径。未显式指定数据库时，新状态保存在 `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite`；若 `/var/lib/mpv-webd/runtime.sqlite` 存在旧数据或无法访问，须明确选择数据库。

媒体根目录使用绝对路径，在 Linux 上以 `:` 分隔。对 `/home` 或 `/var/home` 下已配置的用户目录，自动发现检查 `/media/<account>` 与 `/run/media/<account>` 下直接子目录中的实际挂载点。`MPV_WEBD_MEDIA_MOUNT_PARENT` 可指定一个父目录，`MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` 可关闭发现。设备断开后重新选择媒体根目录。

本地使用时保持回环监听。通过网络访问时，开启认证并允许浏览器使用的精确 Origin。请保管好令牌，在不可信网络上使用加密传输。不要在网络环境启用开发用的免认证覆盖设置。

## 原生渲染器

可选渲染器按[上游说明](../vendor/Omniphony/omniphony-renderer/README.md)构建，并保留资源与组件声明。[补丁来源](../UPSTREAM_PATCHES.md)记录了包含的两项有限输入修复。配置 GPU-next/DRM 原生能力验证时，使用受保护的策略文件，`schema_version` 为 `1`，并填写精确的小写 SHA-256 值 `native_library_sha256` 和 `libplacebo_sha256`。默认路径为 `/etc/mpv-web-suite/native-library-identities.json`，`MPV_WEBD_NATIVE_IDENTITY_POLICY` 可选择其他绝对路径。文件及父目录须归 root 或服务账户所有，组与其他用户不可写。

## 构建发布包

在带有标签且工作区干净的检出目录中安装 build 工具并运行构建脚本。脚本从精确标签归档构建 wheel 和源码发行包，验证提交签名，并将源码、来源记录和校验和写入 Git 忽略的 `build/release/`。输出目录须为空：

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

在输出目录运行 `sha256sum -c SHA256SUMS` 检查文件；使用 `git verify-commit v0.0.1^{commit}` 验证签名提交。附注标签为 `v0.0.1`，标题为 **Fire up**。

## 许可证

应用自有代码采用 **GPL-3.0-only**，见 [LICENSE](../LICENSE)。组件保留原许可证和版权：Omniphony 为 GPL-3.0-or-later，公开参考桥接层为 GPL-3.0-only，SAF/KEMAR 资源为 ISC，Vue 为 MIT。参见[第三方声明](../THIRD_PARTY_NOTICES.md)与组件原始声明。
