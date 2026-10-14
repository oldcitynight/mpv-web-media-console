[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

Un mando desde el navegador para un reproductor libmpv local. Explore medios, gestione la cola, seleccione pistas, cambie la posición y elija salidas de audio y vídeo. La reproducción se ejecuta en el equipo Linux que aloja el servicio.

## Requisitos

Linux, Python 3.11 o posterior y una biblioteca libmpv del sistema compatible y accesible al cargador dinámico. Para compilar la interfaz también se necesitan Node.js 22.12 o posterior y npm. Redis es opcional; configure `MPV_WEBD_REDIS_URL` para utilizarlo.

## Instalación

Desde una copia del código fuente, cree un entorno virtual, compile los recursos del navegador e instale el paquete Python:

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

Para instalar el wheel preparado, active un entorno virtual Python y ejecute lo siguiente en el directorio que contiene el archivo:

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## Configurar e iniciar

Para una instalación nueva, elija un directorio de medios existente y una ruta absoluta de base de datos con permiso de escritura. Sustituya `$HOME/Videos` por su directorio. Ejecute estas variables en la misma sesión que los comandos siguientes:

Antes de actualizar, haga una copia de seguridad e indique en `MPV_WEBD_RUNTIME_DB` la base de datos que desea seguir utilizando. Una ruta distinta crea un estado independiente. Conserve los archivos auxiliares SQLite con la base durante las copias o los traslados.

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

Cree un token de acceso mediante la API del paquete instalado. El comando lo muestra una vez; guárdelo de forma privada y péguelo en el formulario de conexión. No lo incluya en URL, argumentos, capturas ni registros compartidos:

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

Inicie el servicio y abra <http://127.0.0.1:8000>:

```sh
mpv-webd
```

## Uso

Elija inglés o chino simplificado en la interfaz, introduzca el token y conéctese. Library abre las carpetas permitidas; seleccione archivos para añadirlos a Queue o elija Play now. Info contiene los controles y las pistas. En Settings, seleccione un dispositivo de salida y pulse Apply. Actualice la cola antes de repetir una adición no confirmada para evitar duplicados. Detenga el servicio en primer plano con Ctrl+C.

## Configuración portátil

El entorno del proceso tiene prioridad sobre el archivo de configuración. El archivo predeterminado es `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env`; debe ser un archivo normal del usuario del servicio con permisos `0600`. Use líneas `NAME=VALUE` y comillas para los espacios. No guarde credenciales en él. Si define `XDG_STATE_HOME`, debe ser absoluto. Sin ruta explícita, el estado nuevo usa `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite`; si existen datos antiguos o no se puede acceder a `/var/lib/mpv-webd/runtime.sqlite`, es necesario elegir la base explícitamente.

Las raíces multimedia son rutas absolutas separadas por `:` en Linux. Para los hogares configurados en `/home` o `/var/home`, se buscan puntos de montaje reales inmediatamente debajo de `/media/<account>` y `/run/media/<account>`. `MPV_WEBD_MEDIA_MOUNT_PARENT` selecciona otro directorio padre; `MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` desactiva la búsqueda. Vuelva a seleccionar la raíz si se desconecta el dispositivo.

Mantenga la escucha en loopback para uso local. Para acceso por red, active la autenticación y permita el Origin exacto del navegador. Proteja los tokens y cifre el transporte en redes no fiables. Nunca active la excepción de autenticación de desarrollo en una red.

## Renderizador nativo

Compile el renderizador opcional siguiendo las [instrucciones originales](../vendor/Omniphony/omniphony-renderer/README.md) y conserve sus recursos y avisos. La [procedencia de los parches](../UPSTREAM_PATCHES.md) identifica ambas correcciones de entrada finita. Para verificar capacidades nativas GPU-next/DRM, use un archivo de política protegido con `schema_version` igual a `1` y los SHA-256 exactos en minúsculas `native_library_sha256` y `libplacebo_sha256`. La ruta predeterminada es `/etc/mpv-web-suite/native-library-identities.json`; `MPV_WEBD_NATIVE_IDENTITY_POLICY` elige otra ruta absoluta. El archivo y sus directorios padres deben pertenecer a root o al usuario del servicio, sin escritura para el grupo ni para otros.

## Crear una versión

Desde una copia limpia con el tag, instale build y ejecute el script de versión. Crea el wheel y la distribución fuente desde el archivo exacto del tag, verifica la firma del commit y escribe fuentes, procedencia y sumas de control en `build/release/`, ignorado por Git. El directorio de salida debe estar vacío:

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

En el directorio de salida, ejecute `sha256sum -c SHA256SUMS`. Verifique el commit firmado con `git verify-commit v0.0.1^{commit}`. El tag anotado es `v0.0.1`, con título **Fire up**.

## Licencia

El código propio de la aplicación usa **GPL-3.0-only**; consulte [LICENSE](../LICENSE). Los componentes conservan sus licencias y derechos: Omniphony GPL-3.0-or-later, su puente de referencia GPL-3.0-only, el recurso SAF/KEMAR ISC y Vue MIT. Consulte los [avisos de terceros](../THIRD_PARTY_NOTICES.md) y los avisos originales.
