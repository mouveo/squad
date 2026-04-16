# Squad

**Équipe produit IA** — décris une idée, reçois des plans Forge exécutables.

Squad orchestre une équipe de 10 agents IA spécialisés (PM, UX Designer, Architect, Security, Growth, Data Analyst, Customer Success, Delivery, Sales, AI Lead) qui instruisent un sujet produit en 6 phases : cadrage, état des lieux, benchmark concurrentiel, conception, challenge, et synthèse.

## Installation

```bash
pip install -e .
```

## CLI disponible

```bash
# Démarrer une session
squad start <project_path> "idée en quelques mots" [--mode approval|autonomous]

# Voir l'état d'une session ou lister les sessions actives
squad status [session_id]

# Consulter l'historique des sessions
squad history [--project <path>] [--limit 10]

# Version
squad version
```

### Exemple

```bash
squad start ~/Developer/myapp "Ajouter un module CRM avec suivi des leads"
# → Session started: a3f7c1b2-...
# →   Title   : Ajouter un module CRM avec suivi des leads
# →   Mode    : approval
# →   Status  : draft

squad status
# → a3f7c1b2  [draft       ]  —                     Ajouter un module CRM…
```

## Prérequis

- Python 3.11+
- Claude Code CLI (plan Max)

## Roadmap

Les commandes `answer`, `resume`, `review`, `approve` ainsi que l'orchestration multi-phase, la génération de plans Forge et les notifications Slack sont en cours de développement (Plans 2 et 3).

## Licence

Private.
