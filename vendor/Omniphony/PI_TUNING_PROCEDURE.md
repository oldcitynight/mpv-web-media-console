# Procédure de réglage manuel du PI (kp / ki)

Objectif : régler `kp_near` et `ki` du contrôleur adaptive resampling pour obtenir une stabilisation franche et rapide du buffer de latence, sans oscillation.

## Préparation — figer les variables qui masquent le comportement

Avant de toucher kp/ki, neutralise les choses qui peuvent te tromper :

| Paramètre | Valeur de tuning | Raison |
|---|---|---|
| `max_adjust` | **0.05** (5 %) | Sinon le ratio est clampé à 1 % et tu ne vois jamais la vraie réponse en boucle fermée |
| `integral_discharge_ratio` | **0.75** | Le discharge agressif sur sign-change (0.25 par défaut) cache l'effet réel de ki et fausse l'observation |
| `control_smoothing_alpha` | **0.02** (défaut) | Bien : c'est ce qui dégage les bursts décodeur ~300 ms |
| `update_interval_callbacks` | **1** | Réactivité maximale pendant le tuning |

**Setup pratique :**
- Source audio stable et longue (pas de pause vidéo qui déclencherait du low-recover pendant le tuning)
- Telemetry à surveiller :
  - `latencySmoothedMs` (ce que le PI voit réellement)
  - `rate_adjust_ppm`
  - La phase (`stable` / `low-recover` / `settling`)

## Étape 1 — Trouver Kp seul (sans intégrateur)

Objectif : savoir jusqu'où kp peut monter avant que le système oscille.

1. **ki = 0** (P pur, pas d'intégrateur)
2. **kp = 1** au départ
3. Laisse tourner 30 s, note le décalage stable entre `latencySmoothedMs` et `latencyTargetMs` (il y aura un offset résiduel — c'est normal en P pur)
4. **Double kp** : 1 → 2 → 4 → 8 → 16 → 32 → 64… À chaque palier, attends 30 s et observe :
   - Le `rate_adjust_ppm` doit converger rapidement vers une valeur stable
   - Si tu vois `rate_adjust_ppm` osciller avec une période de l'ordre de la seconde ou plus rapide → c'est **kp_crit**
   - Si tu vois `rate_adjust_ppm` saturer à `max_adjust` (50 000 ppm avec ta config), c'est que l'erreur est plus grande que ce que P peut absorber → continue
5. Recule au dernier kp **avant** oscillation : c'est ton **kp_crit**
6. **kp_final = 0.6 × kp_crit** (règle classique pour PI dérivée de Ziegler-Nichols)

> ⚠️ **À noter** : le `max_adjust` clamp les corrections grandes, donc tu peux passer kp_crit sans t'en rendre compte. Surveille bien que `rate_adjust_ppm` ne reste pas pinné à ±50 000.

## Étape 2 — Ajouter Ki

Objectif : éliminer l'erreur résiduelle (offset constant entre buffer et target dû au drift réel).

1. Garde **kp = kp_final**
2. **ki = kp_final / 5** (point de départ typique : Ti ≈ 5 × période d'échantillonnage du contrôleur)
3. Observe sur 1–2 minutes :
   - `latencySmoothedMs` doit converger **vers la target** (plus d'offset)
   - Si tu vois un **dépassement** suivi d'oscillation lente (période de plusieurs secondes) → ki trop fort, divise par 2
   - Si **convergence trop lente** (> 30 s pour rattraper l'offset) → multiplie ki par 2
4. Itère jusqu'à ce que la convergence soit franche (< 10–15 s) sans dépassement notable

## Étape 3 — Test perturbation

Vérifie le comportement post-low-recover :

1. Pause la source 2–3 secondes, reprends
2. Observe :
   - Le low-recover se déclenche, refill rapide, settling, retour stable
   - Le `rate_adjust_ppm` doit revenir à sa valeur compensée en quelques secondes (grâce à la préservation de l'intégrateur)
3. Si l'oscillation post-recovery dure plus de 5–10 s → ki trop fort, baisse de 30 %

## Étape 4 — Test longue durée

10–15 minutes minimum, source stable :

- Pas d'oscillation lente (période > 30 s)
- `rate_adjust_ppm` reste dans une fourchette serrée (±5 ppm autour de la valeur compensée)
- Pas de re-entrées intempestives en low-recover

## Étape 5 — Resserrer la configuration finale

Une fois stable, remettre les protections :

| Paramètre | Valeur finale | Notes |
|---|---|---|
| `max_adjust` | **0.01** (1 %) ou **0.005** | Selon le drift attendu de ton hardware |
| `integral_discharge_ratio` | **0.25** | Discharge agressif aide en cas d'inversion brutale du drift |
| `update_interval_callbacks` | **5–10** | Réduit le bruit de boucle, lisse `rate_adjust` |

Re-tester rapidement après chaque changement (5 minutes minimum, plus un cycle de perturbation).

## Pièges spécifiques à ce système

1. **Unité de kp/ki : ppm/ms.** Avec un drift réel typique de ~100 ppm sur un buffer cible à 200 ms, l'erreur s'exprime en ms et les corrections en ppm. Les valeurs par défaut du studio (`kp=10, ki=50`) injectent **50 ppm par ms d'erreur par seconde** — c'est très agressif et explique potentiellement des overshoots.

2. **`update_interval_callbacks=1`** donne le contrôle le plus fin mais aussi le plus bruité. Si tu vois des spikes sur `rate_adjust_ppm` qui ne sont pas du vrai drift, augmente l'intervalle.

3. **Le sign-change discharge** (qui restera à 0.25 in fine) crée une non-linéarité : la première fois que ton drift change de signe, ki tombe à 25 %. Si pendant le tuning tu vois un comportement qui change radicalement, c'est probablement ça — d'où l'intérêt de le neutraliser temporairement (0.75 ou plus) pendant les étapes 1–2.

4. **Le burst décodeur** est lissé par `CONTROL_SMOOTHING_ALPHA=0.02` (~1 s). Le PI réagit à la version lissée, pas aux pics bruts. Si tu vois quand même du bruit sur `rate_adjust_ppm`, vérifie que tu regardes bien `latencySmoothedMs` et pas `latencyControlMs`.

## Récapitulatif rapide

```
1. Setup : max_adjust=0.05, discharge=0.75, ki=0, kp=1
2. Doubler kp jusqu'à oscillation → kp_final = 0.6 × kp_crit
3. ki = kp_final / 5, ajuster ×2 ou /2 selon convergence
4. Test pause/reprise source : recovery < 5–10 s
5. Test longue durée 10–15 min : pas d'oscillation lente
6. Resserrer : max_adjust=0.01, discharge=0.25, update_interval=5–10
```
