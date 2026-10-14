[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

Um controle pelo navegador para um reprodutor libmpv local. Navegue pelos arquivos, gerencie a fila, selecione faixas, avance na reprodução e escolha saídas de áudio e vídeo. A reprodução ocorre no computador Linux que executa o serviço.

## Requisitos

Linux, Python 3.11 ou superior e uma biblioteca libmpv compatível do sistema, acessível ao carregador dinâmico. Para compilar a interface, também são necessários Node.js 22.12 ou superior e npm. Redis é opcional; configure `MPV_WEBD_REDIS_URL` para utilizá-lo.

## Instalação

Na cópia do código-fonte, crie um ambiente virtual, compile os recursos do navegador e instale o pacote Python:

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

Para instalar o wheel preparado, ative um ambiente virtual Python e execute este comando no diretório que contém o arquivo:

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## Configurar e iniciar

Para uma instalação nova, escolha uma pasta de mídia existente e um caminho absoluto de banco de dados com permissão de escrita. Substitua `$HOME/Videos` pela sua pasta. Defina estas variáveis na mesma sessão dos comandos seguintes:

Antes de atualizar, faça backup e indique em `MPV_WEBD_RUNTIME_DB` o banco de dados que deseja continuar usando. Outro caminho cria um estado separado. Mantenha os arquivos auxiliares do SQLite junto ao banco ao fazer backup ou movê-lo.

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

Crie um token de acesso usando a API do pacote instalado. O comando o mostra uma vez; guarde-o em particular e cole-o no formulário de conexão. Não o coloque em URLs, argumentos de comando, capturas de tela ou registros compartilhados:

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

Inicie o serviço e abra <http://127.0.0.1:8000>:

```sh
mpv-webd
```

## Uso

Escolha inglês ou chinês simplificado na interface, informe o token e conecte. Library abre as pastas permitidas; selecione arquivos para adicionar à Queue ou escolha Play now. Info reúne os controles de reprodução e faixas. Em Settings, escolha um dispositivo e pressione Apply. Atualize a fila antes de repetir uma inclusão não confirmada para evitar duplicatas. Encerre o serviço em primeiro plano com Ctrl+C.

## Configuração portátil

O ambiente do processo tem prioridade sobre o arquivo de configuração. O arquivo padrão é `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env`; ele deve ser um arquivo comum pertencente à conta do serviço, com permissões `0600`. Use linhas `NAME=VALUE` e aspas para espaços. Não armazene credenciais nele. Quando definido, `XDG_STATE_HOME` deve ser absoluto. Sem caminho explícito, o novo estado usa `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite`; dados antigos existentes ou inacessíveis em `/var/lib/mpv-webd/runtime.sqlite` exigem uma escolha explícita do banco.

As raízes de mídia são caminhos absolutos separados por `:` no Linux. Para diretórios pessoais configurados em `/home` ou `/var/home`, a descoberta procura pontos de montagem reais imediatamente sob `/media/<account>` e `/run/media/<account>`. `MPV_WEBD_MEDIA_MOUNT_PARENT` escolhe um diretório pai específico; `MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` desativa a descoberta. Se o dispositivo for desconectado, selecione a raiz novamente.

Mantenha a escuta em loopback para uso local. Para acesso pela rede, ative a autenticação e permita o Origin exato do navegador. Proteja os tokens e use transporte criptografado em redes não confiáveis. Nunca ative a exceção de autenticação de desenvolvimento em uma rede.

## Renderizador nativo

Compile o renderizador opcional seguindo as [instruções originais](../vendor/Omniphony/omniphony-renderer/README.md) e preserve seus recursos e avisos. A [origem dos patches](../UPSTREAM_PATCHES.md) identifica as duas correções de entrada finita incluídas. Para verificar capacidades nativas GPU-next/DRM, use um arquivo de política protegido com `schema_version` igual a `1` e os SHA-256 exatos em minúsculas `native_library_sha256` e `libplacebo_sha256`. O caminho padrão é `/etc/mpv-web-suite/native-library-identities.json`; `MPV_WEBD_NATIVE_IDENTITY_POLICY` seleciona outro caminho absoluto. O arquivo e os diretórios pais devem pertencer a root ou à conta do serviço, sem escrita para o grupo ou outros usuários.

## Gerar uma versão

Na cópia limpa com a tag, instale build e execute o script de versão. Ele gera o wheel e a distribuição-fonte a partir do arquivo exato da tag, verifica a assinatura do commit e grava fonte, procedência e somas de verificação em `build/release/`, ignorado pelo Git. O diretório de saída deve estar vazio:

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

No diretório de saída, execute `sha256sum -c SHA256SUMS`. Verifique o commit assinado com `git verify-commit v0.0.1^{commit}`. A tag anotada é `v0.0.1`, com título **Fire up**.

## Licença

O código próprio da aplicação usa **GPL-3.0-only**; veja [LICENSE](../LICENSE). Os componentes mantêm suas licenças e direitos autorais: Omniphony GPL-3.0-or-later, sua ponte de referência GPL-3.0-only, o recurso SAF/KEMAR ISC e Vue MIT. Consulte os [avisos de terceiros](../THIRD_PARTY_NOTICES.md) e os avisos originais.
