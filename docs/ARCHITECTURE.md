# Architecture de Squad

> Vue d'ensemble des composants stables : pipeline, base de données,
> reprise après crash, benchmark, génération de plans Forge. Lecture
> recommandée avant tout changement structurel.

## Vue 30 secondes

```
            squad CLI (cli.py)
                 │
                 ▼
    pipeline.py  ──►  executor.py  ──►  Claude CLI subprocess
        │                    │
        ▼                    ▼
   workspace.py          DB (sqlite-utils)
   (filesystem)          (squad/db.py)
        │
        ▼
  plan_generator.py ──► forge_format.py ──► forge_bridge.py ──► forge CLI
```

- **CLI** (`squad/cli.py`) — Click. Une commande = un cas d'usage.
- **Pipeline** (`squad/pipeline.py`) — orchestre les 6 phases dans l'ordre.
- **Executor** (`squad/executor.py`) — **seul** point d'appel à Claude CLI.
- **DB** (`squad/db.py`) — SQLite, source de vérité de tout l'état.
- **Workspace** (`squad/workspace.py`) — fichiers de session sur disque.
- **Plan generator + Forge bridge** — sortie : plans markdown poussés
  à Forge.

## Phases

```python
PHASES = [
    "cadrage",         # PM
    "etat_des_lieux",  # CS, Data, Sales, UX
    "benchmark",       # Research (service)
    "conception",      # UX, Architect, Growth, AI Lead
    "challenge",       # Security, Delivery
    "synthese",        # PM
]
```

Chaque phase est exécutée par `pipeline.run_phase`. Le cycle :

1. `phase_config.iter_phases()` donne la config (agents, retry, skip).
2. Pour chaque agent (`executor.run_agent`) :
   - prompt = définition markdown + contexte cumulatif
     (`context_builder`),
   - appel Claude CLI,
   - persistence dans `phase_outputs` (DB) + fichier workspace.
3. Si le contrat de la phase contient des questions pendantes (cas du
   PM en cadrage), la phase **pause** : statut → `interviewing`.
4. Sinon, statut → `working` et on enchaîne.
5. Après `challenge`, si Security/Delivery a produit des blockers
   exploitables, `recovery.can_retry_conception` autorise **un seul**
   retour en `conception` avec une instruction enrichie.

## Statuts d'une session

```
draft ──► working ──► interviewing ──► working ──► review ──► approved ──► queued ──► done
                            │              │           │
                            └──answer/───── ┘           ├── failed (rejet ou erreur)
                              resume                    │
                                                        └── (autonome) ─► queued direct
```

Sources :

- Constantes : `squad/constants.py:STATUS_*`.
- Mises à jour : `db.update_session_status` (loggée, idempotente).
- Lecture : `db.get_session`, `db.list_active_sessions`,
  `db.list_session_history`.

## Persistence (DB SQLite)

Une seule base globale par défaut : `~/.squad/squad.db`. Schéma créé
à la demande par `db.ensure_schema`. Tables principales :

| Table          | Rôle |
|----------------|------|
| `sessions`         | Une ligne par session. Statut, mode, profil de research, IDs externes. |
| `phase_outputs`    | Sortie d'un agent dans une phase (`attempt` permet de filtrer les retries). |
| `questions`        | Questions générées par PM, leurs réponses utilisateur. |
| `plans`            | Plans Forge persistés après validation (`title`, `file_path`, `content`). |

Conventions :

- Pas d'ORM. Les conversions DB → dataclass (`squad/models.py`) vivent
  dans `db._to_session`, `_to_phase_output`, etc.
- `attempt` est entier strictement croissant par (session, phase, agent).
  Le contexte cumulatif n'utilise que le `max(attempt)`.
- Toute écriture passe par une fonction publique de `db.py` —
  pas de SQL inline ailleurs.

## Workspace filesystem

Pour chaque session, Squad écrit aussi sur disque sous
`{projet}/.squad/sessions/{session_id}/` :

```
.squad/sessions/{id}/
├── idea.md
├── context.md
├── 1-cadrage/        ← un fichier par output d'agent
├── 2-etat-des-lieux/
├── 3-benchmark/      ← benchmark-{slug}.md
├── 4-conception/
├── 5-challenge/
├── 6-synthese/
├── questions/
│   └── pending.json  ← miroir lisible des questions ouvertes
└── plans/            ← plans Forge validés
```

Le rôle exact de chaque fichier est codé dans `squad/workspace.py`.

## Reprise après crash ou pause

`squad resume <id>` délègue à `squad/recovery.py:determine_resume_point`.
Le point de reprise dépend du statut :

| Statut courant     | Décision |
|--------------------|----------|
| `interviewing`     | reprend la phase courante après réponse aux questions |
| `working`          | reprend à la phase suivante de la dernière output complète |
| `review`/`done`    | rien à reprendre |
| `failed`           | reprend à la phase qui a échoué (idempotence DB requise) |

Cas particulier : retour en `conception` après blockers Security /
Delivery. `recovery.can_retry_conception` n'autorise **qu'un seul**
retour, tracé en DB (`increment_challenge_retry_count`) — au-delà la
session reste failed pour éviter les boucles infinies.

## Benchmark / research

`squad/research.py` est un agent _service_ (code Python, pas markdown) :

1. `budget_for_depth(depth)` → `ResearchBudget`
   (max axes, max prompt, max output, timeout).
2. `prepare_research_axes(subject_type, depth)` → 3 (`normal`) ou
   5 (`deep`) axes.
3. `load_research_skill()` charge `skills/deep-research/SKILL.md`
   (frontmatter strippée) si présent.
4. `build_research_prompt(...)` assemble le prompt sous budget,
   tronque dans cet ordre : context → protocole skill → prompt complet.
5. `run_research(...)` appelle `executor.run_task_text` avec
   `Read, WebSearch, WebFetch`, persiste dans `research/benchmark-{slug}.md`
   et enregistre une `phase_outputs` row sous `agent="research"`.

Les sessions `light` sautent ce service entièrement
(`subject_detector` met `research_depth="light"`,
`pipeline.should_skip_phase("benchmark", depth)` retourne True).

## Génération de plans Forge

Pipeline complet déclenché en fin de phase synthèse :

1. `plan_generator.generate_plans_from_session(session_id)` :
   - lit la dernière synthèse (max `attempt`),
   - parse le contrat via `phase_contracts.parse_synthesis_contract`,
   - collecte les blockers via `recovery.collect_blocker_constraints`.
2. `build_plan_prompt(...)` charge `templates/forge-plan.md`
   (`load_plan_template()`) et l'injecte sous `## Output format` ;
   le format n'est plus dupliqué dans le code.
3. Appel Claude (`executor.run_task_text`).
4. `forge_format.validate_or_split(content)` :
   - vérifie en-tête, numérotation séquentielle 1..N,
     `**Success criteria**:` + `**Files**:` par lot, bornes 5–15 lots,
   - splits déterministes si > 15 lots,
   - lève `ForgeFormatError` sur tout écart.
5. Chaque plan validé est persisté en DB (`db.create_plan`) et copié
   dans `{projet}/plans/`.

## Soumission à Forge

`squad/forge_bridge.py:submit_session_to_forge(session_id)` :

1. Vérifie la disponibilité de la CLI `forge` (`ForgeUnavailable` sinon).
2. `forge queue add <project> <plan>` pour chaque plan.
3. `forge queue run <project>` une fois — `ForgeQueueBusy` si la queue
   tourne déjà (la session retombe en `review` et notifie via Slack).

Tout passe par cette fonction : ni `cli.py` ni `pipeline.py` ne
shellent `forge` directement.

## Notifications

`squad/notifier.py` envoie un payload JSON sur le webhook Slack
configuré (`SQUAD_SLACK_WEBHOOK` ou `FORGE_SLACK_WEBHOOK`). Évène­ments :

- `notify_questions_pending` / `notify_pause` — pause sur questions.
- `notify_plans_ready` — plans en attente de review.
- `notify_agent_error` — un agent a échoué.
- `notify_fallback_review` — autonome → bascule en review (Forge KO).
- `notify_queued` — plans envoyés à la queue Forge.

## Tests

- `tests/test_*.py` — un fichier par module. Mocks systématiques de
  `executor.run_task_text` / `run_agent`.
- `pytest -m integration` — tests qui appellent vraiment Claude CLI ;
  désactivés en CI.

## Garde-fous architecturaux à respecter

- **Un seul point d'appel à Claude** : `squad/executor.py`. Tout autre
  module qui veut faire un appel doit passer par lui.
- **Un seul point d'écriture en DB** : `squad/db.py`. Pas de SQL ad hoc.
- **Pipeline déterministe sur l'ordre des phases** : modifier `PHASES`
  est un changement structurel (touche `recovery`, `context_builder`,
  `subject_detector`).
- **Pas de second pass post-génération** : `CLAUDE.md` contextualise
  déjà chaque plan ; on ne réécrit pas les lots après-coup.
