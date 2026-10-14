[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

ローカルの libmpv プレーヤーをブラウザーから操作します。メディアの閲覧、キューの管理、トラック選択、シーク、音声・映像出力の選択ができます。再生はサービスを実行する Linux コンピューターで行われます。

## 必要環境

Linux、Python 3.11 以降、および動的ローダーから利用できる互換性のあるシステム libmpv が必要です。ブラウザー画面のビルドには Node.js 22.12 以降と npm も必要です。Redis は任意です。利用する場合は `MPV_WEBD_REDIS_URL` を設定します。

## インストール

ソースをチェックアウトしたディレクトリで仮想環境を作成し、画面のアセットをビルドして Python パッケージをインストールします。

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

用意された wheel を使う場合は Python 仮想環境を有効にし、wheel のあるディレクトリで次を実行します。

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## 設定と起動

新規インストールでは、既存のメディアディレクトリと書き込み可能なデータベースの絶対パスを選びます。`$HOME/Videos` を自分のディレクトリに置き換えてください。以下の変数は後続コマンドと同じシェルで設定します。

更新前にデータベースをバックアップし、引き続き使用するデータベースを `MPV_WEBD_RUNTIME_DB` で指定してください。別のパスを選ぶと独立した状態が作成されます。バックアップや移動時には SQLite の付随ファイルも保存します。

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

インストール済みパッケージの API でブラウザー用アクセストークンを作成します。コマンドが一度表示するトークンを安全に保管し、接続フォームへ貼り付けます。URL、コマンド引数、画面の画像、共有ログに含めないでください。

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

サービスを起動し、<http://127.0.0.1:8000> を開きます。

```sh
mpv-webd
```

## 使い方

画面で英語または簡体字中国語を選び、トークンを入力して接続します。Library で許可されたフォルダーを開き、ファイルを Queue に追加するか Play now を選びます。Info に再生とトラックの操作、Settings に出力設定があります。出力機器を選んで Apply を押してください。追加結果が未確認の場合は、重複を避けるためキューを更新してから再試行します。前面のサービスは Ctrl+C で停止します。

## 移植可能な設定

プロセスの環境変数は設定ファイルより優先されます。既定のファイルは `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env` です。サービスアカウント所有の通常ファイルで、権限を `0600` にします。`NAME=VALUE` 形式を使い、空白のある値は引用符で囲みます。認証情報は保存しないでください。`XDG_STATE_HOME` を設定する場合は絶対パスにします。データベースを指定しない新規環境は `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite` を使います。`/var/lib/mpv-webd/runtime.sqlite` に旧データが存在するかアクセスできない場合は、データベースを明示的に選択します。

メディアのルートは絶対パスで指定し、Linux では `:` で区切ります。`/home` または `/var/home` 配下の設定済みホームに対し、`/media/<account>` と `/run/media/<account>` の直下にある実際のマウントポイントを探します。`MPV_WEBD_MEDIA_MOUNT_PARENT` で親ディレクトリを指定でき、`MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` で探索を無効にできます。機器を切断した場合はルートを選び直します。

ローカル利用ではループバックで待ち受けます。ネットワーク利用では認証を有効にし、ブラウザーの正確な Origin を許可してください。トークンを安全に保ち、信頼できないネットワークでは暗号化通信を使います。開発用の認証無効化設定をネットワークで有効にしないでください。

## ネイティブレンダラー

任意のレンダラーは[上流の手順](../vendor/Omniphony/omniphony-renderer/README.md)でビルドし、リソースとライセンス表記を保持します。[パッチの由来](../UPSTREAM_PATCHES.md)に含まれる有限入力の修正が記載されています。GPU-next/DRM の検証には保護されたポリシーファイルを使い、`schema_version` を `1` にして、正確な小文字 SHA-256 の `native_library_sha256` と `libplacebo_sha256` を指定します。既定パスは `/etc/mpv-web-suite/native-library-identities.json` です。別の絶対パスは `MPV_WEBD_NATIVE_IDENTITY_POLICY` で選択します。ファイルと親ディレクトリは root またはサービスアカウント所有とし、グループやその他のユーザーの書き込みを禁止します。

## リリースのビルド

タグ付きのクリーンなチェックアウトで build をインストールし、リリーススクリプトを実行します。正確なタグのアーカイブから wheel とソース配布を作り、コミット署名を検証し、ソース、由来、チェックサムを Git が無視する `build/release/` に保存します。出力ディレクトリは空にしてください。

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

出力ディレクトリで `sha256sum -c SHA256SUMS` を実行します。署名付きコミットは `git verify-commit v0.0.1^{commit}` で確認できます。注釈付きタグは `v0.0.1`、タイトルは **Fire up** です。

## ライセンス

アプリケーション所有のコードは **GPL-3.0-only** です。[LICENSE](../LICENSE) を参照してください。各コンポーネントのライセンスと著作権は維持されます。Omniphony は GPL-3.0-or-later、参照ブリッジは GPL-3.0-only、SAF/KEMAR は ISC、Vue は MIT です。[第三者の表記](../THIRD_PARTY_NOTICES.md)と元の表記を確認してください。
