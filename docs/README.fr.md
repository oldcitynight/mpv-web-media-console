[English](../README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Português](README.pt.md)

# mpv web suite

Une télécommande dans le navigateur pour un lecteur libmpv local. Parcourez les médias, gérez la file, choisissez les pistes, déplacez la lecture et sélectionnez les sorties audio/vidéo. La lecture se fait sur l’ordinateur Linux qui héberge le service.

## Prérequis

Linux, Python 3.11 ou ultérieur et une bibliothèque système libmpv compatible, accessible au chargeur dynamique. La compilation de l’interface demande aussi Node.js 22.12 ou ultérieur et npm. Redis est facultatif ; définissez `MPV_WEBD_REDIS_URL` pour l’utiliser.

## Installation

Depuis une copie des sources, créez un environnement virtuel, compilez l’interface et installez le paquet Python :

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

Pour installer plutôt le wheel préparé, activez un environnement virtuel Python puis exécutez cette commande dans le dossier contenant le wheel :

```sh
python -m pip install ./mpv_web_suite-0.0.1-py3-none-any.whl
```

## Configurer et démarrer

Pour une nouvelle installation, choisissez un dossier multimédia existant et un chemin absolu de base de données accessible en écriture. Remplacez `$HOME/Videos` par votre dossier. Exécutez ces exports dans le même shell que les commandes suivantes :

Lors d’une mise à niveau, sauvegardez d’abord la base et indiquez dans `MPV_WEBD_RUNTIME_DB` celle que vous souhaitez continuer à utiliser. Un autre chemin crée un état séparé. Conservez les fichiers auxiliaires SQLite avec la base lors d’une sauvegarde ou d’un déplacement.

```sh
export MPV_WEBD_HOST=127.0.0.1
export MPV_WEBD_PORT=8000
export MPV_WEBD_AUTH_ENABLED=true
export MPV_WEBD_ALLOWED_ORIGINS=http://127.0.0.1:8000
export MPV_WEBD_MEDIA_ROOTS="$HOME/Videos"
export MPV_WEBD_RUNTIME_DB="${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite"
```

Créez un jeton d’accès au navigateur avec l’API du paquet installé. La commande l’affiche une fois ; conservez-le de façon privée et collez-le dans le formulaire de connexion. Ne le mettez pas dans une URL, un argument de commande, une capture d’écran ou un journal partagé :

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

Démarrez le service, puis ouvrez <http://127.0.0.1:8000> :

```sh
mpv-webd
```

## Utilisation

Choisissez l’anglais ou le chinois simplifié dans l’interface, saisissez le jeton et connectez-vous. Library ouvre les dossiers autorisés ; sélectionnez des fichiers et ajoutez-les à Queue ou utilisez Play now. Info contient les commandes de lecture et de pistes. Settings permet de choisir une sortie : sélectionnez un appareil puis Apply. Actualisez la file avant de retenter un ajout non confirmé pour éviter les doublons. Arrêtez le service au premier plan avec Ctrl+C.

## Configuration portable

Les variables d’environnement du processus priment sur le fichier de configuration. Le fichier par défaut est `${XDG_CONFIG_HOME:-$HOME/.config}/mpv-webd/mpv-webd.env` ; il doit être un fichier ordinaire appartenant au compte du service, avec les permissions `0600`. Utilisez des lignes `NAME=VALUE` et des guillemets pour les espaces. N’y placez aucun identifiant secret. `XDG_STATE_HOME` doit être absolu. Sans chemin explicite, une nouvelle base utilise `${XDG_STATE_HOME:-$HOME/.local/state}/mpv-webd/runtime.sqlite` ; des données anciennes présentes ou inaccessibles dans `/var/lib/mpv-webd/runtime.sqlite` imposent un choix explicite.

Les racines multimédias sont des chemins absolus séparés par `:` sous Linux. Pour les dossiers personnels configurés sous `/home` ou `/var/home`, la découverte examine les points de montage immédiats réels sous `/media/<account>` et `/run/media/<account>`. `MPV_WEBD_MEDIA_MOUNT_PARENT` choisit un parent précis ; `MPV_WEBD_MEDIA_DISCOVER_MOUNTS=0` désactive la découverte. Sélectionnez de nouveau la racine après une déconnexion du périphérique.

Gardez l’écoute sur loopback pour un usage local. Pour une écoute réseau, activez l’authentification et autorisez l’Origin exact du navigateur. Protégez les jetons et chiffrez le transport sur les réseaux non fiables. N’activez jamais la dérogation d’authentification de développement sur un réseau.

## Moteur de rendu natif

Compilez le moteur facultatif selon les [instructions amont](../vendor/Omniphony/omniphony-renderer/README.md) et conservez ses ressources et mentions de licence. La [provenance des correctifs](../UPSTREAM_PATCHES.md) décrit les deux correctifs d’entrée finie inclus. Pour vérifier les capacités natives GPU-next/DRM, préparez un fichier de politique protégé avec `schema_version` égal à `1` et les SHA-256 exacts en minuscules `native_library_sha256` et `libplacebo_sha256`. Le chemin par défaut est `/etc/mpv-web-suite/native-library-identities.json` ; `MPV_WEBD_NATIVE_IDENTITY_POLICY` choisit un autre chemin absolu. Le fichier et ses dossiers parents doivent appartenir à root ou au compte du service, sans écriture pour le groupe ou les autres.

## Construire une version

Depuis la copie propre portant le tag, installez l’outil build et lancez le script de version. Il crée le wheel et la distribution source depuis l’archive exacte du tag, vérifie la signature du commit et écrit les sources, la provenance et les sommes de contrôle dans `build/release/`, ignoré par Git. Le dossier de sortie doit être vide :

```sh
python -m pip install build
python packaging/build_release.py v0.0.1 --out-dir build/release
```

Dans le dossier de sortie, vérifiez les fichiers avec `sha256sum -c SHA256SUMS`. Vérifiez le commit signé avec `git verify-commit v0.0.1^{commit}`. Le tag annoté est `v0.0.1`, intitulé **Fire up**.

## Licence

Le code appartenant à l’application est sous **GPL-3.0-only** ; voir [LICENSE](../LICENSE). Les composants conservent leurs licences et droits : Omniphony GPL-3.0-or-later, son pont de référence GPL-3.0-only, la ressource SAF/KEMAR ISC et Vue MIT. Consultez les [mentions tierces](../THIRD_PARTY_NOTICES.md) et les mentions originales des composants.
