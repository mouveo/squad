# Squad — Plan 1/3 : Fondations du moteur

> Squad est un orchestrateur multi-agents produit qui transforme une idée en plans Forge exécutables.
> Il fonctionne en CLI Python, utilise Claude Code CLI (Max) comme moteur d'exécution des agents,
> et notifie via Slack quand l'humain est nécessaire.
>
> Ce plan pose les fondations : structure projet, modèle de données, agents, workspace.

---

## LOT 1 — Initialisation du projet Python et structure de base

Créer le projet `squad/` avec la structure suivante :

```
squad/
├── pyproject.toml          # Python 3.11+, click, pyyaml, httpx, sqlite-utils
├── squad/
│   ├── __init__.py
│   ├── cli.py              # Point d'entrée CLI (click)
│   ├── config.py           # Chargement config YAML + env
│   ├── models.py           # Dataclasses / modèles SQLite
│   └── constants.py        # Constantes (phases, statuts, chemins)
├── agents/                 # Définitions des agents (markdown)
├── skills/                 # Skills Squad (pipeline, research)
├── templates/              # Templates de sortie (plan Forge)
├── tests/
│   └── test_cli.py
└── README.md
```

Le CLI doit supporter une commande de base : `squad version` qui affiche la version.

Utiliser `click` pour le CLI (pas argparse). Utiliser `sqlite-utils` pour la DB.

**Success criteria**:
- `pip install -e .` fonctionne
- `squad version` affiche "squad 0.1.0"
- Structure de fichiers conforme
- Tests passent

**Files**: `pyproject.toml`, `squad/__init__.py`, `squad/cli.py`, `squad/config.py`, `squad/models.py`, `squad/constants.py`, `tests/test_cli.py`, `README.md`

---

## LOT 2 — Définition des 10 agents en markdown

Créer un fichier markdown par agent dans `agents/`. Chaque fichier suit exactement cette structure :

```markdown
# Agent: {nom}

## Identité
- Rôle : ...
- Phase d'intervention : cadrage | état_des_lieux | benchmark | conception | challenge | synthèse
- Type : principal | secondaire | contrôle
- Peut poser des questions à l'utilisateur : oui | non

## Mission
{1-2 phrases}

## Réflexes
{liste de 4-6 réflexes}

## Questions clés
{liste de 4-6 questions que cet agent se pose}

## Livrable attendu
{description structurée de ce que l'agent doit produire}

## Erreurs à éviter
{liste de 4-5 anti-patterns}

## Outils autorisés
- web_search: oui|non
- web_fetch: oui|non
- read_files: oui|non
- write_files: oui|non
- execute_commands: oui|non
```

Les 10 agents à créer (basés sur le référentiel `saas_feature_profiles.md` enrichi) :

1. `pm.md` — Product Manager (seul agent autorisé à poser des questions à l'utilisateur)
2. `ux.md` — UX Designer / UX Researcher
3. `architect.md` — SaaS Architect / CTO
4. `security.md` — Security & Compliance Lead
5. `growth.md` — Growth PM / Monetization Strategist
6. `data.md` — Data Analyst / Product Analyst
7. `customer-success.md` — Customer Success / Support Ops Lead
8. `delivery.md` — Engineering Manager / Delivery Lead / QA Lead
9. `sales.md` — Sales / Revenue Ops (NOUVEAU — critique pour CRM, outils B2B)
10. `ai-lead.md` — AI/ML Product Lead (NOUVEAU — pour toute feature IA : prompt design, évaluation, coût, fallback, drift)

IMPORTANT : reprendre le contenu exact des fiches du référentiel pour les agents 1-8, puis créer les agents 9-10 avec le même niveau de détail et de rigueur. L'agent PM doit avoir dans ses réflexes : "Je suis le seul interface avec l'utilisateur. Les autres agents travaillent avec mes inputs et leurs hypothèses."

**Success criteria**:
- 10 fichiers markdown dans `agents/`
- Chaque fichier suit la structure exacte définie
- Le contenu est substantiel (pas de placeholders)
- Les agents 9 et 10 ont le même niveau de détail que les 8 autres

**Files**: `agents/pm.md`, `agents/ux.md`, `agents/architect.md`, `agents/security.md`, `agents/growth.md`, `agents/data.md`, `agents/customer-success.md`, `agents/delivery.md`, `agents/sales.md`, `agents/ai-lead.md`

---

## LOT 3 — Modèle de données et gestion de sessions

Créer le schéma SQLite pour gérer les sessions Squad. Une session = une idée en cours d'instruction.

Tables :

**sessions** :
- `id` TEXT PRIMARY KEY (uuid)
- `project_path` TEXT NOT NULL (chemin du projet cible, ex: ~/Developer/sitavista)
- `idea` TEXT NOT NULL (l'idée brute de l'utilisateur)
- `status` TEXT NOT NULL (draft | interviewing | working | review | approved | queued | done | failed)
- `mode` TEXT NOT NULL DEFAULT 'approval' (approval | autonomous)
- `current_phase` TEXT (cadrage | etat_des_lieux | benchmark | conception | challenge | synthese)
- `created_at` DATETIME
- `updated_at` DATETIME

**phase_outputs** :
- `id` TEXT PRIMARY KEY
- `session_id` TEXT FK
- `phase` TEXT NOT NULL
- `agent` TEXT NOT NULL (nom de l'agent, ex: "pm", "ux")
- `output` TEXT NOT NULL (le livrable markdown produit par l'agent)
- `duration_seconds` REAL
- `tokens_used` INTEGER
- `created_at` DATETIME

**questions** :
- `id` TEXT PRIMARY KEY
- `session_id` TEXT FK
- `agent` TEXT NOT NULL (qui pose la question)
- `phase` TEXT NOT NULL
- `question` TEXT NOT NULL
- `answer` TEXT (NULL tant que non répondu)
- `answered_at` DATETIME
- `created_at` DATETIME

**plans** :
- `id` TEXT PRIMARY KEY
- `session_id` TEXT FK
- `title` TEXT NOT NULL
- `content` TEXT NOT NULL (le plan markdown complet au format Forge)
- `forge_status` TEXT (pending | queued | running | done | failed)
- `created_at` DATETIME

Implémenter dans `squad/models.py` avec `sqlite-utils`. La DB est stockée dans `{project_path}/.squad/squad.db`. Créer aussi `squad/db.py` avec les fonctions CRUD de base pour chaque table.

**Success criteria**:
- Schéma créé automatiquement au premier usage
- Fonctions CRUD testées pour sessions, phase_outputs, questions, plans
- Tests unitaires couvrent création, lecture, mise à jour de statut

**Files**: `squad/models.py`, `squad/db.py`, `tests/test_db.py`

**Depends on**: LOT 1

---

## LOT 4 — Workspace de session et système de fichiers

Quand une session démarre, Squad crée un workspace sur le filesystem :

```
{project_path}/.squad/sessions/{session_id}/
├── idea.md                    # L'idée brute
├── context.md                 # Contexte projet (auto-généré depuis CLAUDE.md si présent)
├── phases/
│   ├── 1-cadrage/
│   │   └── pm.md              # Output du PM
│   ├── 2-etat-des-lieux/
│   │   ├── ux.md
│   │   ├── data.md
│   │   └── customer-success.md
│   ├── 3-benchmark/
│   │   └── research.md
│   ├── 4-conception/
│   │   ├── ux.md
│   │   ├── architect.md
│   │   └── growth.md
│   ├── 5-challenge/
│   │   ├── security.md
│   │   ├── delivery.md
│   │   └── ai-lead.md         # Si feature IA détectée
│   └── 6-synthese/
│       └── pm.md
├── questions/
│   └── pending.json           # Questions en attente de réponse
├── plans/
│   ├── plan-1-xxx.md          # Plans Forge générés
│   └── plan-2-xxx.md
└── research/
    └── benchmark-*.md         # Résultats de recherche web
```

Implémenter `squad/workspace.py` :
- `create_workspace(session_id, project_path, idea)` : crée l'arborescence
- `write_phase_output(session_id, phase, agent, content)` : écrit le livrable d'un agent
- `read_phase_outputs(session_id, phase)` : lit tous les livrables d'une phase (pour que la phase suivante les consomme)
- `write_plan(session_id, plan_title, plan_content)` : écrit un plan Forge
- `get_context(project_path)` : lit le CLAUDE.md du projet cible s'il existe, sinon retourne un contexte minimal

**Success criteria**:
- Arborescence créée correctement
- Lecture/écriture des outputs fonctionne
- Le contexte projet est extrait du CLAUDE.md
- Tests couvrent tous les cas

**Files**: `squad/workspace.py`, `tests/test_workspace.py`

**Depends on**: LOT 3

---

## LOT 5 — Moteur d'exécution d'agent via Claude Code CLI

C'est le cœur du système. Implémenter `squad/executor.py` qui lance un agent via Claude Code CLI.

Un agent est exécuté ainsi :
1. Squad construit un prompt qui contient :
   - Le system prompt de l'agent (depuis `agents/{agent}.md`)
   - Le contexte projet (depuis `context.md`)
   - L'idée originale
   - Les outputs des phases précédentes (si applicable)
   - Les réponses aux questions (si applicable)
   - La consigne de livrable attendu
2. Squad appelle Claude Code CLI en mode non-interactif :
   ```bash
   claude --print --output-format stream-json \
     --model claude-opus-4-6 \
     --allowedTools "WebSearch,WebFetch,Read" \
     --prompt "{prompt_complet}"
   ```
3. Squad capture le stream NDJSON, extrait le texte final
4. Squad écrit le résultat dans le workspace et la DB

Implémenter :
- `build_agent_prompt(agent_name, session_id, phase, extra_context=None)` : construit le prompt complet
- `run_agent(agent_name, session_id, phase)` : exécute et retourne le résultat
- `run_agents_parallel(agents_list, session_id, phase)` : lance plusieurs agents en parallèle (asyncio.gather ou concurrent.futures)

IMPORTANT : utiliser `--allowedTools` pour restreindre les outils selon la définition de l'agent. L'agent Research a accès à WebSearch+WebFetch. Les agents de conception n'ont que Read. Le PM a accès à tout.

Gérer les erreurs : timeout (15 min max par agent), erreur CLI, output vide. En cas d'échec, retry 1 fois puis marquer comme failed.

**Success criteria**:
- Un agent peut être exécuté et son output capturé
- Les agents parallèles fonctionnent
- Le prompt contient bien le contexte, l'idée, et les outputs précédents
- Gestion d'erreur et timeout fonctionnels
- Test d'intégration avec un agent simple (peut être mocké pour le CI)

**Files**: `squad/executor.py`, `tests/test_executor.py`

**Depends on**: LOT 2, LOT 4

---

## LOT 6 — Notifications Slack

Implémenter `squad/notifier.py` pour envoyer des messages Slack via webhook.

Réutiliser la même variable d'environnement que Forge : `FORGE_SLACK_WEBHOOK` (ou `SQUAD_SLACK_WEBHOOK` si défini, avec fallback sur Forge).

Types de notifications :

1. **Questions en attente** : "🎯 Squad — Session `{title}` : {n} question(s) à valider. Lancer `squad answer {session_id}` pour répondre."
2. **Plans prêts** : "📋 Squad — Session `{title}` : {n} plan(s) Forge générés. Lancer `squad review {session_id}` pour valider."
3. **Plans envoyés à Forge** : "🚀 Squad → Forge — {n} plan(s) envoyés dans la queue Forge pour `{project}`."
4. **Erreur** : "❌ Squad — Erreur sur `{title}` phase `{phase}` agent `{agent}` : {message}"

Chaque notification inclut un timestamp et le session_id pour traçabilité.

**Success criteria**:
- Messages envoyés via webhook HTTP POST
- Fallback silencieux si pas de webhook configuré (log warning, pas de crash)
- Test avec mock HTTP

**Files**: `squad/notifier.py`, `tests/test_notifier.py`

**Depends on**: LOT 1

---

## LOT 7 — CLI : commandes principales

Implémenter les commandes CLI dans `squad/cli.py` :

```bash
# Démarrer une nouvelle session
squad start <project_path> "idée en quelques mots" [--mode autonomous]

# Répondre aux questions en attente
squad answer <session_id>
# Affiche les questions une par une, attend les réponses en stdin

# Voir le statut d'une session
squad status [session_id]
# Sans argument : liste toutes les sessions actives

# Voir les plans générés
squad review <session_id>
# Affiche les plans, demande validation (y/n/edit)

# Approuver et envoyer à Forge
squad approve <session_id> [--forge-auto]
# --forge-auto : envoie directement dans la queue Forge sans demander

# Reprendre une session en pause (après réponse aux questions)
squad resume <session_id>

# Historique
squad history [--project <path>] [--limit 10]
```

Pour `squad start` :
1. Crée la session en DB
2. Crée le workspace
3. Lance la phase 1 (cadrage PM)
4. Si le PM a des questions → les stocke, notifie Slack, passe en status "interviewing"
5. Si pas de questions → enchaîne les phases suivantes

Pour `squad answer` :
1. Charge les questions pending
2. Les affiche une par une avec le contexte (quel agent, quelle phase)
3. Enregistre les réponses
4. Passe le status à "working"
5. Appelle `squad resume` automatiquement

Pour `squad approve` :
1. Lit les plans de la session
2. Pour chaque plan, appelle `forge queue add <project> <plan.md>`
3. Met à jour le forge_status en DB
4. Notifie Slack

**Success criteria**:
- Toutes les commandes fonctionnent
- `squad start` lance effectivement le pipeline
- `squad answer` est interactif et fonctionnel
- `squad approve` interface correctement avec Forge CLI
- Help text clair pour chaque commande

**Files**: `squad/cli.py`, `tests/test_cli_commands.py`

**Depends on**: LOT 3, LOT 4, LOT 5, LOT 6

