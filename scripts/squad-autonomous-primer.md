# Squad — primer autonome pour IA collaboratrice

> Tu lis ce fichier depuis une conversation dans un projet quelconque.
> L'objectif : piloter Squad **sans intervention humaine** via la CLI
> bash. Lis-moi en entier avant toute action.

## En 4 commandes

```bash
# 1. Préflight — vérifier que les services sont up
pgrep -f "squad serve" >/dev/null && echo "serve OK" || echo "serve DOWN"
claude --print --max-turns 1 "hi" >/dev/null && echo "claude OK"
ls ~/Developer/<projet>/CLAUDE.md >/dev/null && echo "projet OK"

# 2. Prépare ton brief dans le bon dossier
mkdir -p ~/Developer/<projet>/plans/<sujet>/
# Place tes .md/.txt/.csv dedans (1 à 10 fichiers, ≤10 Mo chacun)

# 3. Lance la session — ÉCRIS le path explicite `plans/<sujet>` dans l'idée
squad run <projet> "<idée qui mentionne plans/<sujet>>"

# 4. Suis la progression, approuve quand status=review
squad status
squad review <session_id>
squad approve <session_id>
```

## Règle critique zéro-guesswork

Les documents sont importés automatiquement **uniquement si l'idée
contient textuellement** `plans/<sujet>` ou `plans/<sujet>/file.md`.
Pas de token-matching, pas de devinette. Une idée sans `plans/...`
produit une session sans attachments — même si le dossier existe.

**Bon** : `squad run ressort "Module whaou — voir plans/whaou"`
**Mauvais** : `squad run ressort "Module whaou"` (pas de path explicite)

## Ce que Squad fait avec ton brief

7 phases en 30-50 min, avec Opus 4.7 1M :
cadrage → état des lieux → idéation → benchmark → conception → challenge → synthèse.

Sortie : un plan Forge `.md` écrit dans `{projet}/plans/<titre-slug>.md`.
Format validé automatiquement (5-15 lots, success criteria, files).

## Si l'idée + les docs joints sont riches

Squad détecte automatiquement (`input_richness=rich`) et :
- Saute la pause ideation (auto_pick le meilleur angle)
- Dampening sur le benchmark ("combles les angles morts au lieu de refaire")

Pour forcer cette richesse : idée > 500 chars **ET/OU** au moins 1
doc texte > 3000 chars dans `plans/<sujet>/`.

## Approuver → Forge exécute

`squad approve <session_id>` met le status à `queued` et lance Forge
en arrière-plan (non-bloquant). Forge crée une worktree, exécute lot
par lot, commit chaque lot sur une branche `forge/<plan>-<date>`.
Tu observes via `forge queue list <projet>`.

## Suivi en direct

- `squad status [<session_id>]` — état + phase courante
- `tail -f ~/.squad/serve.log` — logs bruts
- `http://localhost:8501` — dashboard Streamlit (auto-refresh 5s)

## Options CLI utiles

```bash
squad run <projet> "<idée>" --mode autonomous       # auto-approve + auto-submit
squad run <projet> "<idée>" --no-plans-autoscan     # bypass autoscan ponctuel
squad start <projet> "<idée>"                       # lance sans suivre, rend la main
squad resume <session_id>                           # reprend après crash/pause
squad review <session_id> --action reject --reason "…"  # rejet explicite
```

## Erreurs typiques à ne pas faire

- ❌ Dire que Squad n'accepte pas de documents (il accepte via `plans/<sujet>/`)
- ❌ Oublier le path explicite dans l'idée → rien d'importé
- ❌ Lancer un `squad run` pendant qu'une session est en `working` (risque de collision queue)
- ❌ Tuer `squad serve` pendant qu'un pipeline tourne (pipeline orphelin, voir TROUBLESHOOTING.md)

## Docs complémentaires

- `docs/FOR_AI_COLLABORATORS.md` — guide détaillé (anatomie prompt/doc, exemples canoniques)
- `docs/SITAVISTA_TEST.md` — exemple concret end-to-end
- `docs/TROUBLESHOOTING.md` — diagnostics quand ça coince
- `scripts/preflight.sh` — check exécutable des prérequis
