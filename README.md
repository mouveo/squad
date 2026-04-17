# Squad

**Équipe produit IA** — décris une idée, reçois des plans Forge exécutables.

Squad orchestre 10 agents IA spécialisés (PM, UX Designer, Architect,
Security, Growth, Data Analyst, Customer Success, Delivery, Sales,
AI Lead) qui instruisent un sujet produit en 6 phases — cadrage, état
des lieux, benchmark, conception, challenge, synthèse — puis
matérialisent la décision en plans Forge prêts à exécuter.

## Quick start

```bash
# 1. Installer
pip install -e .

# 2. (Optionnel) Initialiser la config globale
squad init

# 3. Lancer une session interactive sur un projet existant
squad run ~/Developer/myapp \
  "Ajouter un module CRM avec suivi des leads et scoring automatique"
```

`squad run` enchaîne tout : création de session → pipeline → questions
inline → reprise → review → soumission à Forge. En `--mode autonomous`
les questions sont sautées et la soumission se fait sans validation
humaine.

Pour un flux asynchrone (lancer maintenant, répondre plus tard), voir la
section [Commandes](#commandes).

## Installation

```bash
pip install -e .
```

Prérequis :

- Python 3.11+
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installé
  et authentifié (Squad délègue toute la génération d'agents au binaire
  `claude`).
- (Optionnel) `forge` CLI sur le PATH si on veut auto-soumettre les
  plans à la queue d'exécution Forge.

## Commandes

| Commande | Rôle |
|----------|------|
| `squad run <project> "<idée>" [--mode …]`    | One-shot interactif : start → questions → review → submit. |
| `squad start <project> "<idée>" [--mode …]`  | Crée la session et lance le pipeline ; rend la main quand le pipeline pause ou termine. |
| `squad answer <session_id> <question_id> "<réponse>"` | Répond à une question pending de façon asynchrone. |
| `squad resume <session_id>`                   | Reprend une session paused/crashed à la prochaine phase sûre. |
| `squad review <session_id> [--action …]`      | Affiche / approuve / rejette / édite les plans générés. |
| `squad approve <session_id>`                  | Pousse les plans approuvés à la queue Forge. |
| `squad status [session_id]`                   | Détail d'une session ou liste des sessions actives. |
| `squad history [--project <p>] [--limit N]`   | Historique des sessions terminées. |
| `squad init [--project <p>] [--force]`        | Écrit un YAML de config par défaut (global ou projet). |
| `squad version`                               | Version installée. |

Les commandes asynchrones (`start` + `answer` + `resume` + `review` +
`approve`) restent la base — `squad run` les enchaîne dans un seul
appel pour les workflows interactifs.

## Modes d'exécution

- **`approval`** (défaut) — Squad pause et demande confirmation aux
  endroits définis : questions générées par les agents puis validation
  des plans avant soumission Forge.
- **`autonomous`** — toutes les interactions sont sautées. Sur arrivée
  en `review`, la soumission à Forge est automatique. En cas d'erreur
  Forge, la session retombe en `review` avec une notification Slack.

Le mode est résolu dans cet ordre :

1. Flag CLI `--mode` explicite.
2. Clé `mode` dans la config projet (`{projet}/.squad/config.yaml`).
3. Clé `mode` dans la config globale (`~/.squad/config.yaml`).
4. Fallback : `approval`.

## Configuration

Squad lit deux fichiers YAML, fusionnés en deep-merge :

- **Global** : `~/.squad/config.yaml` (créé par `squad init`).
- **Projet** : `{projet}/.squad/config.yaml` (créé par
  `squad init --project <projet>`). Les clés écrasent celles du global.

Les `${VAR}` sont résolus contre l'environnement au chargement.

Exemple de config minimale :

```yaml
mode: autonomous

slack:
  webhook: ${SQUAD_SLACK_WEBHOOK}

pipeline:
  agent_timeout: 900
```

## Intégration Forge

Squad génère les plans au format Forge (`## LOT N — Titre`,
`**Success criteria**:`, `**Files**:`) et les valide via
`squad/forge_format.py` avant tout. La soumission utilise :

```bash
forge queue add <project> <plan.md>
forge queue run  <project>
```

Voir `docs/ARCHITECTURE.md` pour le contrat entre Squad et Forge.

## Coût et garde-fous

- **Modèle par défaut** : `claude-opus-4-6`. Les phases légères
  (classification, résumé) basculent sur Sonnet quand c'est possible.
- **Budget research** : `ResearchBudget` impose un nombre max d'axes
  (3 normal, 5 deep), un cap sur le prompt et la sortie, et un
  timeout par sous-tâche.
- **Timeout par agent** : 15 minutes max ; au-delà, l'agent est
  marqué failed.
- **Coût indicatif** : compter ~5–15 USD pour une session complète
  (idée moyenne, profil normal). Les vraies métriques sont consignées
  dans `examples/{projet}/summary.md` après chaque validation manuelle.
- **Reprise** : chaque transition de statut est persistée en SQLite —
  `squad resume <id>` reprend exactement à la phase suivante après
  crash ou pause.
- **Pas de secrets** : ni dans les plans, ni dans les prompts, ni dans
  les logs.

## Documentation

- `CLAUDE.md` — instructions pour Claude Code travaillant sur Squad.
- `AGENTS.md` — rôles internes (Architecte / Dev / Testeur / Rédacteur).
- `docs/AGENTS.md` — les 10 agents produit que Squad orchestre.
- `docs/ARCHITECTURE.md` — pipeline, DB, recovery, benchmark, génération.
- `docs/TUNING.md` — décisions d'ajustement issues des validations manuelles.
- `examples/` — sessions archivées (résumés et métriques uniquement).

## Licence

Privé.
