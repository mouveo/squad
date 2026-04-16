# Squad

**Équipe produit IA** — décris une idée, reçois des plans Forge exécutables.

Squad orchestre une équipe de 10 agents IA spécialisés (PM, UX Designer, Architect, Security, Growth, Data Analyst, Customer Success, Delivery, Sales, AI Lead) qui instruisent un sujet produit en 6 phases : cadrage, état des lieux, benchmark concurrentiel, conception, challenge, et synthèse.

Le résultat : des plans markdown prêts à être exécutés par [Forge](../forge/), l'exécuteur autonome de plans.

## Quick start

```bash
# Install
pip install -e .

# Initialiser la config
squad init

# Lancer sur un projet
squad start ~/Developer/sitavista "améliorer le CRM : leads, pipeline, scoring"

# Ou en mode interactif complet (one-shot)
squad run ~/Developer/sitavista "améliorer le CRM : leads, pipeline, scoring"
```

## Comment ça marche

```
"améliorer le CRM"
       │
       ▼
┌─────────────────────────────────────────┐
│  Phase 1 — Cadrage (PM)                │
│  Problème, segment, pourquoi maintenant │
│  → Questions Slack si besoin            │
├─────────────────────────────────────────┤
│  Phase 2 — État des lieux (parallèle)  │
│  UX · Data · CS · Sales                │
├─────────────────────────────────────────┤
│  Phase 3 — Benchmark                   │
│  Recherche web concurrentielle          │
├─────────────────────────────────────────┤
│  Phase 4 — Conception (parallèle)      │
│  UX · Architect · Growth · AI Lead     │
├─────────────────────────────────────────┤
│  Phase 5 — Challenge (garde-fous)      │
│  Security · Delivery · FinOps          │
├─────────────────────────────────────────┤
│  Phase 6 — Synthèse (PM)              │
│  Résumé décisionnel + plans Forge      │
└──────────────┬──────────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
  Mode approval    Mode autonome
  (Slack notif)    (→ Forge queue)
       │               │
       └───────┬───────┘
               ▼
        forge queue run
          → code mergé
```

## Les 10 agents

| Agent | Rôle | Phases |
|-------|------|--------|
| **PM** | Cadrage, priorisation, synthèse | 1, 6 |
| **UX Designer** | Parcours, frictions, simplification | 2, 4 |
| **Architect** | Faisabilité, patterns, scalabilité | 4 |
| **Security** | RGPD, permissions, abus | 5 |
| **Growth** | Funnel, monétisation, limites | 4 |
| **Data Analyst** | Métriques, tracking, KPI | 2 |
| **Customer Success** | Tickets, friction support | 2 |
| **Delivery** | Découpage, tests, rollback | 5 |
| **Sales** | Usage terrain, objections, CRM | 2 |
| **AI Lead** | Prompt design, coût, fallback | 4 |

## Modes

**Approval** (défaut) : Squad notifie via Slack quand les plans sont prêts. Tu valides avant envoi à Forge.

**Autonomous** : les plans sont envoyés directement dans la queue Forge. Pour les projets en début de vie où tu veux itérer vite.

```bash
# Démarrer en mode autonome
squad start ~/Developer/monprojet "ajouter l'export CSV" --mode autonomous
```

## Configuration

Config globale : `~/.squad/config.yaml`
Config projet : `{projet}/.squad/config.yaml` (surcharge la globale)

## CLI

```bash
squad init                          # Créer la config globale
squad start <projet> "idée"         # Démarrer une session
squad run <projet> "idée"           # Mode interactif complet
squad answer <session_id>           # Répondre aux questions
squad resume <session_id>           # Reprendre après pause
squad review <session_id>           # Voir et valider les plans
squad approve <session_id>          # Envoyer les plans à Forge
squad status [session_id]           # Statut des sessions
squad history                       # Historique
```

## Prérequis

- Python 3.11+
- Claude Code CLI (plan Max)
- Forge installé (pour l'envoi des plans)
- Slack webhook configuré (optionnel, pour les notifications)

## Licence

Private.
