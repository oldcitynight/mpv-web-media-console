# Algorithme De Régulation De La Latence

Ce document décrit la régulation actuelle de la latence de sortie temps réel dans `omniphony-renderer`, avec un focus sur le modèle de contrôle partagé et sur les différences backend entre `ASIO` et `PipeWire`.

## Objectifs

Le contrôleur de latence a quatre rôles principaux :

1. Maintenir la sortie audible proche d'une latence cible configurée.
2. Récupérer proprement après une dérive basse ou haute du buffer, sans laisser fuiter d'audio instable.
3. Supporter le resampling adaptatif local lorsqu'il est activé.
4. Exposer assez d'état à l'UI pour rendre les recoveries observables.

La cible de long terme n'est pas "la latence la plus basse possible". La cible est "une latence stable proche du setpoint demandé, avec un comportement de recovery prévisible".

## Modèle Central

La logique de régulation partagée vit dans [adaptive_runtime.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/adaptive_runtime.rs).

### Domaines

Deux domaines d'échantillons sont importants :

- Domaine d'entrée : échantillons décodés/rendus écrits dans le ring buffer du backend.
- Domaine de sortie : échantillons réellement consommés par le callback backend après resampling local.

Le contrôle de latence est volontairement exprimé dans le domaine d'entrée, pour que le contrôleur raisonne sur le même stock audio indépendamment du sample rate de sortie.

### Grandeurs Mesurées

À chaque callback, le backend calcule :

- `available_input_samples` : remplissage courant du ring buffer.
- `output_fifo_input_domain_samples` : contenu du FIFO du resampler local reconverti en samples du domaine d'entrée.
- `callback_input_domain_samples` : taille du callback reconvertie dans le domaine d'entrée.
- `control_available` : `ring + output_fifo - callback/2`.
- `control_latency_ms` : `control_available / (sample_rate * channels)`.
- `measured_latency_ms` : `control_latency_ms + estimation de latence graphe/backend`.

`control_latency_ms` est la quantité utilisée pour la régulation. `measured_latency_ms` est l'estimation totale affichée à l'utilisateur.

### Remplissage Cible

La latence cible est convertie en niveau de remplissage cible :

- target fill = `target_latency_ms * input_sample_rate * channel_count / 1000`

Ce niveau de remplissage est le centre du contrôleur.

## Machine D'État De Recovery Partagée

La machine d'état de recovery expose les états UI :

- `stable`
- `low-recover`
- `settling`
- `high-recover`

### Low Recovery

Le low recovery est utilisé quand le buffer tombe trop en dessous de la cible.
Il est **activé** par le switch `hard_recover_low_in_far_mode` (ou par le
startup), et l'entrée en Refill se déclenche dès que
`control_available < target - low_recover_entry_margin_ms` — c'est le seul
déclencheur côté bas (il n'est plus conditionné par la bande far, voir Near/Far).

Progression :

1. `stable -> low-recover` (Refill)
2. `low-recover -> settling`
3. `settling -> stable`

Pendant `low-recover` (phase Refill), la sortie est mutée et le ring se
remplit. La sortie de Refill est **prédictive** : on passe en `settling` dès
que `control_available` — ou sa projection via l'EMA de vitesse de remplissage
`low_recover_refill_delta_ema` — atteint `target - low_recover_exit_margin_ms`.

**Pendant tout le low-recover (Refill et Settling), la servo PI est
désactivée** (gate `low_recover_phase == Inactive` dans `pipewire.rs` et
`asio.rs`) et le ratio est figé au ratio de base : aucune correction d'horloge
n'est appliquée tant qu'on n'est pas revenu en `stable`.

### Settling

`settling` existe pour éviter de rouvrir l'audio immédiatement après le refill. Le but est de rendre la latence effective de retour moins aléatoire.

Comportement actuel :

- la sortie est mutée si `force_silence_in_far_mode` est activé (défaut), sinon elle redevient audible pendant le settling
- si le niveau retombe sous `target - low_recover_settle_margin_ms`, retour en `low-recover`
- si le niveau dépasse `target + low_recover_settle_margin_ms`, on trim l'excès
- si le niveau reste assez longtemps dans la fenêtre de settling, transition vers `stable`

Temps de sortie actuel :

- `low_recover_settle_stable_ms` (défaut `200 ms`) de temps stable **cumulé d'affilée** dans la fenêtre ; toute sortie de bande réarme le compteur

Demi-fenêtre de settling actuelle :

- `low_recover_settle_margin_ms` (défaut `6 ms`), convertie en samples et alignée sur la frame audio

#### Raw vs smoothed (anti dent-de-scie)

Le test des bornes du dwell de settling se fait sur la **valeur lissée**
(`smoothed_control_available`, le même IIR passe-bas que celui vu par la servo
PI), **pas** sur la valeur brute. Les entrées/exits (entrée en Refill, sortie
prédictive de Refill, hard-recover) restent sur la valeur **brute** pour rester
réactives — les lisser réintroduisait un lag de phase qui provoquait une
oscillation lente.

Raison : l'arrivée de l'entrée par bursts (batching du décodeur) crée une
**dent de scie** sur `control_available` dont l'amplitude dépasse la
demi-fenêtre `±low_recover_settle_margin_ms`. Jugé sur la valeur brute, chaque
dent fait sortir de bande et réarme le compteur → le warmup n'atteint jamais
`stable` (alors qu'une fois en `stable`, la servo, qui travaille déjà sur le
lissé, tient sans problème). Jugé sur le lissé, la dent de scie est absorbée et
le dwell peut mûrir.

À la transition `Refill -> Settling`, l'état de l'IIR est **réinitialisé** pour
que le lissé reparte du niveau réel courant et non d'une valeur encore en
retard sur la rampe de refill (ce qui rebondirait aussitôt en Refill).

### High Recovery

Le high recovery est utilisé quand le buffer dépasse trop la cible.

Comportement :

- on jette agressivement de l'audio bufferisé pendant le mute
- on revient vers la cible plus vite que par la seule servo lente

## Logique Near/Far

La bande `near/far` est dérivée de l'erreur de buffer par rapport à la cible :

- `near` si `abs(control_available - target_fill) < high_recover_entry_margin_ms`
- `far` sinon

> Renommage : `near_far_threshold_ms` s'appelle désormais
> **`high_recover_entry_margin_ms`** (« High-recover entry margin »), pour
> former une paire claire avec `low_recover_entry_margin_ms`. Le seuil est
> **symétrique** (`abs_diff`), mais pour une latence cible réaliste il ne peut
> être atteint que **côté haut** ; c'est donc l'entrée des actions high-side
> (hard-recover-high, mute far). Les anciens noms (config disque, clé JSON
> `nearFarThresholdMs`, adresse OSC) restent acceptés en lecture via des alias.

Cette bande sert à la fois pour l'UI et pour décider si les actions far-mode **côté haut** sont éligibles. L'entrée en **low-recover** n'utilise plus cette bande : elle se déclenche sur `low_recover_entry_margin_ms` (côté bas) dès que `hard_recover_low_in_far_mode` (ou le startup) est actif — voir la section Low Recovery.

La distinction importante est :

- la bande indique à quelle distance on est de la cible
- l'état de recovery indique ce que la machine de recovery est réellement en train de faire

Les deux sont liés, mais ce n'est pas la même information.

## Resampling Adaptatif Local

Quand le resampling adaptatif est activé, une servo PI décale légèrement le ratio du resampler local autour du ratio de base.

La logique partagée vit dans :

- [lib.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/lib.rs)
- [adaptive_runtime.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/adaptive_runtime.rs)

Entrées :

- remplissage de contrôle courant
- remplissage cible
- gains configurés `kp_near`, `ki`
- `max_adjust`
- `integral_discharge_ratio`

Sorties :

- ratio effectif du resampling local
- valeur affichée de rate-adjust
- bande adaptative courante (`near` ou `far`)

La boucle PI n'est qu'une partie du système. Elle ne remplace pas les hard recoveries. Elle essaie de recentrer le système avant qu'un hard recovery devienne nécessaire.

## Comportement Au Démarrage

### ASIO

Le démarrage ASIO réutilise maintenant la machine d'état normale de low recovery au lieu d'utiliser un pre-fill gate dédié.

Flux actuel :

1. le stream démarre muté en `low-recover`
2. le refill se fait avec la même logique que pour un low-buffer recovery classique
3. `settling` stabilise la latence de retour
4. transition vers `stable`

En plus, quand le recovery de démarrage se termine, ASIO reset explicitement :

- l'état interne du resampler local
- le FIFO du resampler

et garde encore un callback muté avant de rendre le premier bloc audible. Le but est d'éviter qu'un transitoire de démarrage accumulé dans l'état du resampler ne fuie vers la sortie.

### PipeWire

PipeWire force lui aussi le low-recover de démarrage : `activate_startup_low_recover()` est appelé à la création du stream (`pipewire.rs`), exactement comme ASIO. Le démarrage suit donc la même machine d'état Refill → Settling → stable. La différence avec ASIO porte sur la cadence des callbacks (pilotée par le quantum du graphe) et sur la mesure de latence, pas sur la présence du gate de démarrage.

## Différences ASIO / PipeWire

C'est la section backend spécifique la plus importante.

### 1. Modèle De Callback

`ASIO` :

- la taille de callback est déterminée par le driver / backend CPAL
- elle peut être relativement grossière et très dépendante du driver
- cela rend les seuils de recovery plus sensibles à la granularité du callback

`PipeWire` :

- la cadence des callbacks est liée au quantum du graphe
- elle est en général plus régulière
- cela facilite le tuning du settling et de la servo

### 2. Mesure De Latence

`ASIO` :

- n'a pas actuellement de vraie mesure directe de latence graphe/driver
- utilise une estimation de milieu de callback
- la latence totale affichée est donc un modèle, pas une valeur driver mesurée

`PipeWire` :

- échantillonne la latence graphe downstream via `pw_stream_get_time()`
- inclut un vrai délai de scheduling du graphe dans `measured_latency_ms`

C'est pour ça que deux backends peuvent sembler aussi stables à l'oreille tout en affichant des chiffres de latence différents.

### 3. Comportement Sans Resampling

`ASIO` :

- sans resampling adaptatif local, il repose toujours sur la logique de recovery far-mode partagée
- il n'y a pas d'équivalent séparé à la servo backend native de PipeWire

`PipeWire` :

- a deux régimes :
  - chemin avec resampler local
  - chemin avec servo native backend quand le resampler local n'est pas utilisé

Donc PipeWire est structurellement plus flexible, mais les deux backends ne sont pas des miroirs exacts.

### 4. Stratégie De Démarrage

`ASIO` :

- le démarrage est maintenant explicitement traité comme un low recovery
- le mute / recovery / fade suit volontairement la même logique qu'une recovery low classique

`PipeWire` :

- force lui aussi le low-recover de démarrage (`activate_startup_low_recover()`), même machine d'état que ASIO
- la cadence de callback du graphe rend juste le refill/settling plus régulier à tuner

### 5. Sensibilité Aux Seuils

`ASIO` est plus sensible à :

- la largeur de fenêtre de settling
- les seuils de transition refill / settling
- le nettoyage des transitoires de démarrage

`PipeWire` est plus sensible à :

- la taille de quantum du graphe
- la qualité de la mesure de latence backend
- la séparation entre contrôle par resampler local et contrôle natif backend

## Interprétation Pratique Actuelle

Quand on debug le système, il faut interpréter les états comme suit :

- `stable` : aucune machine de recovery active
- `low-recover` : la sortie est mutée parce que le système reconstruit la latence depuis un buffer trop bas
- `settling` : le système confirme la stabilité (sur le niveau lissé) avant de rendre la main à la servo ; la sortie est mutée si `force_silence_in_far_mode` est activé (défaut)
- `high-recover` : de l'audio bufferisé est jeté parce que la latence est trop haute
- `near` / `far` : distance à la cible, pas état de mute en soi

Si l'audio se comporte mal, il faut toujours regarder à la fois :

- la bande : `near` / `far`
- l'état : `stable` / `low-recover` / `settling` / `high-recover`

La bande explique où le contrôleur se situe par rapport à la cible. L'état explique ce que la machine de recovery est réellement en train de faire.
