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

## Intégration avec Forge

Squad produit des plans markdown au format Forge (`## LOT N — Titre`). Ces plans sont :
1. Écrits dans `{project_cible}/plans/`
2. Envoyés via `forge queue add {project} {plan.md}`
3. Exécutés via `forge queue run {project}`

Le format Forge est décrit dans les templates (`templates/forge-plan.md`) et validé par `forge_format.py`.

## Points d'attention

- **Coût API** : chaque session Squad consomme des tokens. Le modèle par défaut est Opus (puissant mais coûteux). Pour les tâches légères (classification, résumé), utiliser Sonnet.
- **Timeout** : un agent a 15 minutes max. Au-delà, il est considéré comme failed.
- **Contexte cumulatif** : chaque phase reçoit les outputs des phases précédentes. Attention à ne pas dépasser ~15k tokens de contexte.
- **Resumabilité** : le pipeline doit pouvoir reprendre après un crash. Chaque transition d'état est persistée en DB.
- **Pas de secrets** : ni dans les plans générés, ni dans les prompts, ni dans les logs.
