# Squad v1 — Archive documentaire

Ce document fige l'état de Squad v1 avant la migration v2. Le tag annoté
`squad-v1-final` pointe vers le dernier commit de cette version. Tous les
fichiers listés ci-dessous restent récupérables via :

```bash
git show squad-v1-final:<path>
```

Exemple : `git show squad-v1-final:agents/ideation.md > /tmp/ideation.md`.

## Agents v1 (11)

| Agent              | Rôle                          | Fichier source                  |
|--------------------|-------------------------------|---------------------------------|
| `pm`               | Product Manager               | `agents/pm.md`                  |
| `ux`               | UX Designer                   | `agents/ux.md`                  |
| `architect`        | Software Architect            | `agents/architect.md`           |
| `security`         | Security Engineer             | `agents/security.md`            |
| `growth`           | Growth Strategist             | `agents/growth.md`              |
| `data`             | Data Analyst                  | `agents/data.md`                |
| `customer-success` | Customer Success Manager      | `agents/customer-success.md`    |
| `delivery`         | Delivery Lead                 | `agents/delivery.md`            |
| `sales`            | Sales Strategist              | `agents/sales.md`               |
| `ai-lead`          | AI Lead Engineer              | `agents/ai-lead.md`             |
| `ideation`         | Product Ideation Strategist   | `agents/ideation.md`            |

## Phases v1 (7) et verdict v2

| Phase             | Rôle v1                                                          | Verdict v2                                                                 |
|-------------------|------------------------------------------------------------------|----------------------------------------------------------------------------|
| `cadrage`         | Cadrage initial du sujet par PM                                  | Conservée                                                                  |
| `etat_des_lieux`  | Diagnostic multi-agents (UX, Data, Customer Success, Sales)      | Fusionnée dans une phase de diagnostic resserrée                           |
| `ideation`        | Génération d'angles produits par l'agent ideation                | Supprimée — l'idéation est intégrée au cadrage et au benchmark             |
| `benchmark`       | Recherche externe (deep-research skill)                          | Conservée                                                                  |
| `conception`      | Conception multi-agents (UX, Architect, AI Lead, Growth)         | Recentrée sur UX + Architect uniquement                                    |
| `challenge`       | Pression critique (Architect, Security, Delivery)                | Conservée mais sans agents séparés (checklist intégrée)                    |
| `synthese`        | Synthèse finale et génération des plans Forge par PM             | Conservée                                                                  |

## Organisation post-cleanup

Agents runtime conservés dans le pipeline v2 :

- `pm` — orchestration cadrage et synthèse
- `ux` — diagnostic UX et conception
- `architect` — conception et challenge technique

Agent conservé temporairement hors pipeline :

- `security.md` — gardé comme source pour la conversion en checklist de
  challenge intégrée. Retiré du pipeline runtime, à supprimer une fois la
  checklist extraite.

Agents et phases retirés du runtime v2 (récupérables via le tag) :

- Agents : `growth`, `data`, `customer-success`, `delivery`, `sales`,
  `ai-lead`, `ideation`.
- Phase : `ideation` (l'agent et la phase portent le même nom).

## Récupération

Pour restaurer un fichier ou inspecter l'état v1 :

```bash
# Lister les fichiers à un path donné dans le tag
git ls-tree -r squad-v1-final -- agents/

# Récupérer un agent
git show squad-v1-final:agents/ideation.md > agents/ideation.md

# Diff entre v1 et HEAD
git diff squad-v1-final HEAD -- agents/
```
