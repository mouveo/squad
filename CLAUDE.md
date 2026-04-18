# Squad — Instructions pour Claude Code

> Ce fichier est lu automatiquement par Claude Code à chaque lot Forge.
> Il décrit le projet, la stack, les conventions et les règles à suivre.

## Projet

Squad est un orchestrateur multi-agents produit. Il prend une idée en entrée, la fait instruire par une équipe d'agents IA spécialisés (PM, UX, Architect, Security, etc.), et produit des plans exécutables par Forge.

Squad est un outil CLI Python destiné à être utilisé par un product owner technique. Il n'a pas d'interface web (Forge a déjà un dashboard). Il communique avec l'utilisateur via le terminal et Slack.

## Stack

- **Langage** : Python 3.11+
- **CLI** : Click
- **DB** : SQLite via sqlite-utils
- **Config** : YAML (PyYAML)
- **HTTP** : httpx (pour Slack webhooks et appels éventuels)
- **Moteur d'exécution des agents** : Claude Code CLI (`claude` en ligne de commande)
- **Tests** : pytest
- **Formatage** : ruff (lint + format)

## Structure du projet

```
squad/
├── pyproject.toml
├── CLAUDE.md              ← ce fichier
├── AGENTS.md              ← rôles pour travailler sur Squad lui-même
├── README.md
├── squad/
│   ├── __init__.py        # version
│   ├── cli.py             # commandes Click
│   ├── config.py          # chargement config YAML
│   ├── constants.py       # constantes (phases, statuts, chemins)
│   ├── models.py          # dataclasses
│   ├── db.py              # CRUD SQLite
│   ├── workspace.py       # gestion filesystem des sessions
│   ├── executor.py        # exécution d'un agent via Claude CLI
│   ├── context_builder.py # construction du prompt cumulatif
│   ├── pipeline.py        # orchestrateur de phases
│   ├── phase_config.py    # config des phases (agents, parallélisme)
│   ├── subject_detector.py # classification du sujet
│   ├── research.py        # agent research spécialisé
│   ├── plan_generator.py  # génération de plans Forge
│   ├── forge_format.py    # validation et formatage Forge
│   ├── forge_bridge.py    # pont vers Forge CLI (queue, run)
│   ├── recovery.py        # reprise après crash/pause
│   └── notifier.py        # notifications Slack
├── agents/                # définitions des agents (1 markdown par agent)
├── skills/                # skills Claude Code embarqués
├── templates/             # templates de sortie
├── scripts/               # scripts utilitaires (install-skills.sh)
├── tests/                 # pytest
├── docs/                  # documentation détaillée
└── examples/              # sessions archivées comme exemples
```

## Conventions de code

### Python
- Python 3.11+ (match/case autorisé, type hints obligatoires)
- Fonctions typées : paramètres et retour
- Dataclasses pour les modèles (pas de Pydantic, on reste léger)
- Docstrings sur les fonctions publiques (format Google)
- Pas de classes quand une fonction suffit
- Imports absolus uniquement (`from squad.db import create_session`, pas de relatifs)
- Constantes en UPPER_SNAKE_CASE dans `constants.py`
- Logging via `logging` standard (pas de print)

### Nommage
- Fichiers : snake_case
- Classes : PascalCase
- Fonctions et variables : snake_case
- Agents markdown : kebab-case (`customer-success.md`)
- Plans Forge générés : `plan-N-titre-slug.md`

### Tests
- Un fichier de test par module : `tests/test_{module}.py`
- Utiliser pytest fixtures pour la DB et le workspace
- Mocker les appels Claude CLI dans les tests unitaires (pas d'appels réels)
- Les tests d'intégration qui appellent vraiment Claude sont marqués `@pytest.mark.integration`

### Gestion d'erreurs
- Pas de try/except silencieux (toujours logger l'erreur)
- Les erreurs CLI affichent un message clair à l'utilisateur via `click.echo`
- Les erreurs critiques (PM fail, DB corrompue) arrêtent le pipeline
- Les erreurs non-critiques (agent secondaire fail) sont loguées et le pipeline continue

### Git
- Commits en anglais, impératif : "Add pipeline orchestrator", "Fix agent timeout handling"
- Un commit par lot Forge (géré automatiquement par Forge)

## Intégration avec Claude Code CLI

Squad appelle Claude Code CLI pour exécuter les agents. Voici le pattern standard :

```bash
claude --print --output-format stream-json \
  --model claude-opus-4-6 \
  --allowedTools "WebSearch,WebFetch,Read" \
  --prompt "..."
```

Options importantes :
- `--print` : mode non-interactif, output sur stdout
- `--output-format stream-json` : NDJSON pour parser le stream
- `--allowedTools` : restreindre les outils selon le profil de l'agent
- `--model` : claude-opus-4-6 par défaut, claude-sonnet-4-6 pour les tâches légères (classification)

Le stream NDJSON contient des lignes JSON avec `type: "text"` pour le contenu. Extraire et concaténer toutes les lignes `type: "text"` pour obtenir le résultat final.

### Exploration active des projets cibles

Certains agents ont besoin de lire les fichiers du projet cible pour produire un diagnostic ou une conception fidèle à la réalité du code. Cette capacité est **sélective** :

- Seuls `ux` et `architect` ont `Glob`, `LS`, `Grep` activés dans leur section `## Outils autorisés` (voir `agents/ux.md` et `agents/architect.md`). Les autres agents restent cantonnés à `Read` (et, selon les cas, `WebSearch`/`WebFetch`).
- Pour ces deux agents, `pipeline._run_agents` route `cwd=session.project_path` vers le sous-processus Claude via `executor._call_claude_cli(..., cwd=...)`. Concrètement : `run_agent(..., cwd=project_path)` en séquentiel, `run_agents_tolerant(..., cwd_by_agent={agent: cwd, ...})` en parallèle.
- Le helper `pipeline._resolve_agent_cwd(session, agent)` n'applique ce `cwd` que si l'agent est dans `_AGENTS_WITH_PROJECT_CWD = {"ux", "architect"}` et que le chemin existe. Sinon il retombe sur `cwd=None` (avec un warning quand le chemin est déclaré mais absent).
- Le contexte cumulatif (`build_cumulative_context`) reste partagé par phase et inclut déjà un pré-scan du projet (`CLAUDE.md`, `README`, manifests, tree, `git log`) : `Glob`/`LS`/`Grep` servent à affiner, pas à re-cartographier.

## Intégration avec Forge

Squad produit des plans markdown au format Forge (`## LOT N — Titre`). Ces plans sont :
1. Écrits dans `{project_cible}/plans/`
2. Envoyés via `forge queue add {project} {plan.md}`
3. Exécutés via `forge queue run {project}`

Le format Forge est décrit dans le template (`templates/forge-plan.md`), chargé par `plan_generator.py` et validé par `forge_format.py` (lots numérotés 5–15, `**Success criteria**:` + `**Files**:` obligatoires).

## Configuration utilisateur

- Global : `~/.squad/config.yaml` (créé par `squad init`).
- Projet : `{projet}/.squad/config.yaml` (créé par `squad init --project <projet>`), deep-merged sur le global.
- Variables `${VAR}` résolues à la lecture (`squad/config.py:load_config`).
- Le défaut est défini dans `DEFAULT_CONFIG_YAML` ; les helpers `load_config` / `get_config_value` sont les seuls points d'entrée.

## Skills Claude Code

Le skill canonique vit dans `skills/deep-research/SKILL.md`. Il est :
- chargé par `squad/research.py` (`load_research_skill`) et injecté dans le prompt benchmark ;
- installable côté utilisateur via `scripts/install-skills.sh` (cible `~/.claude/skills` par défaut, idempotent).

## Points d'attention

- **Coût API** : chaque session Squad consomme des tokens. Le modèle par défaut est Opus (puissant mais coûteux). Pour les tâches légères (classification, résumé), utiliser Sonnet.
- **Timeout** : un agent a 15 minutes max. Au-delà, il est considéré comme failed.
- **Contexte cumulatif** : chaque phase reçoit les outputs des phases précédentes. Attention à ne pas dépasser ~15k tokens de contexte.
- **Resumabilité** : le pipeline doit pouvoir reprendre après un crash. Chaque transition d'état est persistée en DB.
- **Pas de secrets** : ni dans les plans générés, ni dans les prompts, ni dans les logs.
