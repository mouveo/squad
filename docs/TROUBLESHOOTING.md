# Squad — Troubleshooting

Journal des pièges rencontrés en conditions réelles et des contre-mesures
déjà codées. Mise à jour quand un nouveau cas tombe.

## Où regarder en premier

1. **`~/.squad/serve.log`** — logs persistants de `squad serve` (rotation
   à 5 MB × 3). Chaque event `file_shared`, chaque transition de phase,
   chaque retry agent y passe. Fichier ouvrable en direct avec
   `tail -f ~/.squad/serve.log`.
2. **`squad status`** et **`squad history`** — statut DB des sessions.
3. **DB brute** : `sqlite3 ~/.squad/squad.db` pour des requêtes ciblées
   (ex: `SELECT id, status, failure_reason FROM sessions ORDER BY
   created_at DESC LIMIT 10;`).

## Cas rencontrés

### `squad serve` crashe silencieusement (avant fix 3eb23f6)

**Symptôme** : le processus `squad serve` meurt avec exit code 1, aucune
trace dans les task outputs.

**Cause racine** : `run_serve` n'appelait pas `logging.basicConfig` avant
`handler.start()`. Tout log Slack Bolt (disconnect, token refresh, crash
handler) partait dans un handler par défaut absorbé par le shell
détaché.

**Fix actif** depuis `3eb23f6` :
- `configure_logging(log_file)` installe un StreamHandler + RotatingFileHandler
- Supervisor loop avec backoff exponentiel (5s → 10s → … cap 10min)
- Heartbeat toutes les 5 min (`Heartbeat — pipelines running: N`)
- Signal handlers SIGTERM/SIGINT pour shutdown propre

**Si le problème réapparaît** : vérifier que `~/.squad/serve.log` est
bien créé. Lancer avec `squad serve --log-file /tmp/squad-debug.log
--heartbeat-minutes 1` pour un test plus verbeux.

### Fichier Slack ignoré (avant fix f45f80a + cdd62ce)

**Symptôme** : drag-drop d'un fichier dans le thread d'une session,
l'attachment n'arrive jamais dans `{workspace}/attachments/` et aucun
accusé dans le thread.

**Causes possibles (diagnostiquées par les logs)** :

1. **Le fichier n'est pas envoyé** — il reste dans le brouillon
   Slack (bouton envoi pas cliqué, `⌘+Return` nécessaire). Le compose
   box affiche encore la carte du fichier.
2. **Le fichier est déposé dans le channel principal, pas dans un
   thread** — fix cdd62ce : fallback auto-attach à la session récente
   (< 120s) sur le même channel.
3. **Le fichier est déposé dans un thread qui n'a pas de session**
   — fix f45f80a : warning posté dans le thread pour guider l'user.
4. **Session déjà failed/approved** — la fenêtre d'attachement est
   fermée ; poster le fichier dans une nouvelle session.

**Diagnostic** : `grep "file_shared" ~/.squad/serve.log | tail -10`
donne le file_id, le channel, la décision de routage (matched,
fallback, ignored) et la raison.

### Pipeline orphelin après restart de `squad serve`

**Symptôme** : une session reste bloquée en `working/cadrage` (ou
autre phase), rien ne progresse.

**Cause** : `squad serve` héberge un ThreadPoolExecutor qui exécute les
pipelines. Si le process est tué (kill -9, crash), les threads meurent
avec. La DB garde `status=working` mais plus aucun thread ne travaille.

**Fix actif** : SIGTERM (kill sans -9) attend la fin propre des
pipelines avant de sortir. Le heartbeat expose `pipelines running: N`.

**Récupération manuelle** :
- `squad resume <session_id>` — reprend depuis le point sûr déterminé
  par `recovery.py`
- OU `sqlite3 ~/.squad/squad.db "UPDATE sessions SET status='failed',
  failure_reason='orphaned' WHERE id='<uuid>';"` pour annuler puis
  repartir sur une nouvelle session.

### Upload du `.md` du plan absent dans Slack → `missing_scope: files:write`

**Symptôme** : quand la review est postée dans le thread, le résumé
apparaît mais le fichier `.md` complet n'est pas attaché. Dans
`serve.log` :
```
slack_sdk.errors.SlackApiError: {'ok': False, 'error': 'missing_scope',
  'needed': 'files:write', 'provided': 'commands,chat:write,...,files:read,...'}
```

**Cause** : l'app Slack créée au LOT 1 du plan 4 a bien `files:read`
(pour télécharger les attachments utilisateur) mais pas `files:write`
(pour uploader des fichiers côté bot).

**Fix côté config Slack** (une seule fois) :

1. Ouvrir https://api.slack.com/apps/<app_id>/oauth
2. Dans "Bot Token Scopes", cliquer "Add an OAuth Scope"
3. Ajouter `files:write`
4. En haut de la page, cliquer "Reinstall to Workspace" — le token `xoxb-` reste le même mais les nouveaux scopes sont activés
5. Pas besoin de redémarrer `squad serve` — les scopes sont résolus à chaque appel Slack API

Bot Token Scopes complets attendus :
```
commands, chat:write, files:read, files:write,
app_mentions:read, im:read, im:write
```

### Authentification Claude CLI 401 intermittent

**Symptôme** : Forge ou Squad rate un appel Claude avec
`API Error: 401 "Invalid authentication credentials"`. Peut arriver
sur quelques lots d'affilée puis revenir spontanément.

**Contre-mesures** :
- `claude --print "hi" --max-turns 1` pour tester l'auth de la CLI
- Si KO : `claude` en interactif pour relancer l'auth
- Pour Forge queue : `forge queue clear <project>` + re-add les plans,
  pas besoin de tout recoder depuis zéro — ce qui est déjà committé
  dans les branches `forge/*` reste exploitable

### `research_depth` manquant → benchmark skippé silencieusement

**Symptôme** : `ValueError: Session has no research_depth; run the
subject detector first`. Le benchmark se skippe, les plans sortent
plus pauvres.

**Cause** : `detect_and_persist` n'est pas appelé sur les sessions
Slack (uniquement sur les sessions CLI historiquement).

**Fix** : plan 7 LOT 2 — `run_pipeline` force la classification avant
toute phase. En attendant le merge, contournement manuel :
```python
from squad.subject_detector import detect_and_persist
detect_and_persist("<session_id>", use_llm=True)
```
puis `squad resume <session_id>`.

### Contexte cumulatif qui explose (80k+ chars)

**Symptôme** : warning `Cumulative context … exceeds target`. Les
agents reçoivent un prompt tronqué au-delà de la fenêtre, sortent du
format attendu (pas de bloc JSON de contrat, angles non parseables).

**Fix** : plan 7 LOT 5 — ceiling + résumé adaptatif des phases
anciennes dans `build_cumulative_context`. Les attachments et les 2
phases les plus récentes ne sont jamais compressées.

**Avant merge** : augmenter le modèle à `claude-opus-4-7[1m]` (1M
tokens) dans `~/.squad/config.yaml` (`model: claude-opus-4-7[1m]`)
débloque temporairement.

## Commandes d'audit

```bash
# Sessions des dernières 6h avec leur cause d'échec
sqlite3 ~/.squad/squad.db "SELECT substr(id,1,8), status, current_phase, failure_reason FROM sessions WHERE datetime(created_at) >= datetime('now','-6 hours') ORDER BY created_at DESC;"

# Heartbeats et phases dans les logs
grep -E "Heartbeat|Running phase|Pipeline failed" ~/.squad/serve.log | tail -20

# Taille et contenu des attachments d'une session
ls -la ~/Developer/<project>/.squad/sessions/<uuid>/attachments/
```
