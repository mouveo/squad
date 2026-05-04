# Test end-to-end Sitavista — procédure propre

Script reproductible pour lancer une vraie session Squad sur
`~/Developer/sitavista` avec la deepsearch CRM pré-posée dans
`sitavista/plans/<subject>/`, et vérifier que le pipeline sort des
plans Forge exécutables.

À faire **après merge des plans 7 (hardening), 8 (dashboard) et 9
(plans auto-scan)**.

La procédure principale s'appuie sur l'auto-scan
`{project}/plans/<subject>/` (Plan 9) : Squad détecte le dossier dès
que l'idée contient le token `<subject>`, importe les `.md/.txt/.csv`
avant la première phase, et affiche un résumé. Le drag-drop Slack
reste le fallback pour les fichiers qui n'ont pas encore été posés
côté filesystem.

## Pré-flight

Une seule commande pour vérifier que tout est prêt :

```bash
cd ~/Developer/squad
./scripts/preflight.sh  # à créer — voir template ci-dessous
```

Checks manuels si pas encore de script :

1. **Main à jour** : `git status` propre, `git log --oneline -10` montre
   les plans 7 et 8 mergés.
2. **Venv à jour** : `.venv/bin/pip install -e ".[slack,dashboard,dev]"`
   et `.venv/bin/pytest -m "not integration" --tb=no -q` passe.
3. **Claude CLI auth** : `claude --print "hi" --max-turns 1` répond.
4. **Sitavista présent** : `ls ~/Developer/sitavista/CLAUDE.md` existe.
5. **Deepsearch pré-posée dans `plans/<subject>/`** :
   ```bash
   mkdir -p ~/Developer/sitavista/plans/crm
   cp ~/Developer/sitavista/plans/deep-research/deep-research-report-crm-comparison-sitavista-vs-leaders.md \
      ~/Developer/sitavista/plans/crm/
   ls ~/Developer/sitavista/plans/crm/
   ```
   Le token `crm` (ou `deepsearch`, `refonte`, etc. — tout mot ≥ 3
   caractères que tu mettras dans l'idée) doit être présent dans
   l'idée ci-dessous pour déclencher l'auto-scan.
6. **Slack config** : `SQUAD_SLACK_BOT_TOKEN` et `SQUAD_SLACK_APP_TOKEN`
   dans l'env ; `~/.squad/config.yaml` contient `bot_token:
   ${SQUAD_SLACK_BOT_TOKEN}` et `app_token: ${SQUAD_SLACK_APP_TOKEN}`.
7. **Pas de serve zombie** : `pgrep -f "squad serve" | wc -l` retourne
   `0` ou 1. Si >1, `pkill -f "squad serve"` + restart.

## Démarrage serve

Dans un terminal dédié (pas en background Claude Code) :

```bash
cd ~/Developer/squad
.venv/bin/squad serve
```

Vérifier dans `~/.squad/serve.log` :
- `Starting Slack Socket Mode`
- `⚡️ Bolt app is running!`
- Un heartbeat apparaît dans les 5 min.

## Prompt à coller dans Slack

Dans `#squad-chat` (ou n'importe quel channel où `@Squad` est invité),
taper simplement la commande suivante **sans rien glisser** : la
deepsearch est déjà dans `sitavista/plans/crm/` et sera auto-attachée
dès que l'idée contient `crm`.

```
/squad new Refonte progressive du CRM Sitavista pour combler le gap
fonctionnel et UX avec Pipedrive, HubSpot et Monday CRM, en
conservant les 3 avantages différenciants : modèle natif
Network→Agency multi-tenant, module RDV riche, copilote IA
contextuel. Une deepsearch est disponible dans plans/crm/ : audit
actuel, benchmark feature-par-feature, analyse UX/UI, reco priorisée,
pièges à éviter, pricing. Exploite-la comme source de vérité — ne
refais pas la recherche web, ne ré-audite pas les concurrents. Stack
: Laravel 13 + Inertia v2 + React 18 + TypeScript + Tailwind v4 +
shadcn/ui, RBAC 9 rôles, Redis tags invalidation. Trajectoire cible
recommandée : 1) fiche record refondue HubSpot-like avec 3 colonnes,
tabs, cards collapsibles ; 2) pipeline cockpit avec preview drawer +
quick actions ; 3) activities workspace global ; 4) email v1
intégré (1:1 + templates + tracking) ; 5) devis v1 lié à opportunité
(lignes + TVA + PDF + e-signature) ; 6) workflow automation v1 avec
8-12 recettes métier prêtes ; 7) command palette Cmd+K orientée
action. À éviter : cloner HubSpot en largeur, omnichannel inbox, CPQ
complet, workflow builder open-ended. Produis des plans Forge
exécutables par lots atomiques.
```

**Ce qui se passe après envoi** :
1. Squad crée la session et poste le message racine dans `#squad-chat`.
2. L'auto-scan détecte `sitavista/plans/crm/` (token `crm` dans l'idée)
   et importe tous les `.md` présents.
3. Le thread reçoit :
   `:open_file_folder: N fichier(s) auto-attaché(s) depuis plans/crm
   — 0 rejeté, 0 ignoré`.
4. Le pipeline démarre avec la deepsearch déjà dans le contexte.

### Fallback — drag-drop Slack

Si la deepsearch n'existe pas encore côté filesystem (fraîchement
exportée depuis un outil externe, par exemple), le workflow
historique reste disponible :

**Fichier à joindre** :
`/Users/olivier/Developer/sitavista/plans/deep-research/deep-research-report-crm-comparison-sitavista-vs-leaders.md`

Grâce au fix `cdd62ce` (auto-attach < 120s), le fichier sera injecté
dans la session même s'il arrive avant que le bot ait créé le thread.

### Variante CLI

Même logique, sans Slack :

```bash
cd ~/Developer
squad run sitavista "Refonte progressive du CRM Sitavista — exploite la deepsearch dans plans/crm/…"
# ou pour bypasser l'auto-scan ponctuellement :
squad run sitavista "…" --no-plans-autoscan
```

Le CLI imprime `Auto-scan : N importé(s), 0 rejeté(s), 0 ignoré(s)
depuis …` avant de démarrer le pipeline.

## Checkpoints attendus

| Étape | Signal visible | Durée typique |
|---|---|---|
| Session créée | Message éphémère + post public `[Squad] Session créée — <uuid>` avec `Projet: /Users/olivier/Developer/sitavista` | < 3s |
| Fichier attaché | Message `:paperclip: Fichier <name> attaché (… octets)` dans le thread, OU log `file_shared received` dans serve.log puis `Attachment stored` | < 10s |
| Classification sujet | `subject_type` et `research_depth` persistés en DB (sqlite check) | < 30s |
| Phase Cadrage | `🎯 Phase : Cadrage` dans le thread | ~2 min |
| Phase État des lieux | Message suivant, agent ux lancé avec exploration active sur sitavista | 3-5 min |
| Phase Benchmark | Research exploite la deepsearch, **pas** de WebSearch redondant sur HubSpot/Pipedrive/Monday | 5-10 min |
| Phase Conception | ux + architect en parallèle avec exploration active sur sitavista | 5-10 min |
| Phase Challenge | architect produit le contrat blockers (challenge séquentiel mono-agent en v2) | 5-10 min |
| Phase Synthèse | PM produit le markdown final avec bloc JSON de contrat | 5-10 min |
| Plans générés | Messages dans le thread avec résumé + fichier `.md` uploadé | < 30s |
| Total | | ~45-75 min |

## Vérifications DB en cours de route

```bash
# Suivi live
watch -n 10 'sqlite3 ~/.squad/squad.db "SELECT substr(id,1,8) id, status, current_phase FROM sessions ORDER BY created_at DESC LIMIT 1;"'

# Attachments présents
ls -la ~/Developer/sitavista/.squad/sessions/$(sqlite3 ~/.squad/squad.db "SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1;")/attachments/

# Phases déjà sorties (outputs)
sqlite3 ~/.squad/squad.db "SELECT phase, agent, attempt, datetime(created_at) FROM phase_outputs WHERE session_id = (SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1) ORDER BY created_at;"
```

## Dashboard Streamlit (après merge plan 8)

Ouvert en parallèle dans un autre terminal :

```bash
.venv/bin/squad dashboard
```

Puis navigateur sur `http://localhost:8501`. Sessions listées,
timeline live, plans review avec boutons approve/reject.

## En cas d'échec

Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Les trois cas les plus
probables :

1. **Pipeline bloqué sur une phase pendant > 20 min** : `tail -f
   ~/.squad/serve.log` pour voir les retry agent, puis
   `squad resume <session_id>` si nécessaire.
2. **Plans générés mais format invalide** : vérifier la sortie de la
   phase synthese dans `{workspace}/phases/06-synthese/pm.md` ; le
   parser tolérant de plan 7 LOT 4 doit éviter ça, sinon récupérer
   manuellement.
3. **Fichier non attaché** : vérifier `serve.log` pour l'event
   `file_shared`. Si absent, Slack ne l'a pas envoyé — re-drop dans le
   thread (matching par thread_ts).

## Après success

1. `squad review <session_id>` pour lister les plans.
2. Ouvrir chaque `.md` généré dans `~/Developer/sitavista/plans/` pour
   validation manuelle avant soumission Forge.
3. `forge queue add sitavista plans/<plan>.md` pour chaque plan
   retenu, puis `forge queue run sitavista`.
4. Documenter le coût total et la qualité perçue dans
   `examples/sitavista/summary.md` pour le tuning futur.
