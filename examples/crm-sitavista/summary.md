# Sitavista CRM — résumé de validation

> **Statut : à compléter après exécution manuelle.** Ce fichier est un
> squelette structuré ; les valeurs marquées `TBD` doivent être
> renseignées par l'opérateur juste après le `squad run`. Aucun chiffre,
> aucun extrait, aucune décision ne doit être inventé ici.

## Métadonnées

| Champ                    | Valeur |
|--------------------------|--------|
| Date de session          | TBD (AAAA-MM-JJ) |
| Commit Squad évalué      | TBD (`git rev-parse HEAD`) |
| Modèle par défaut        | TBD (`claude-opus-4-6` ou config locale) |
| Mode                     | `approval` |
| Durée totale (min)       | TBD |
| Coût estimé (USD)        | TBD |
| Statut final             | TBD (`review` / `approved` / `failed`) |

Commande exécutée :

```bash
squad run ~/Developer/sitavista \
  "améliorer le CRM : gestion des leads, pipeline de vente, scoring automatique, relances"
```

## Note par phase (1–3 lignes)

| Phase           | Verdict (OK / À surveiller / KO) | Note |
|-----------------|----------------------------------|------|
| Cadrage         | TBD | TBD |
| État des lieux  | TBD | TBD |
| Benchmark       | TBD | TBD |
| Conception      | TBD | TBD |
| Challenge       | TBD | TBD |
| Synthèse        | TBD | TBD |
| Plans Forge     | TBD | TBD |

Référence des critères : voir `docs/TUNING.md`, section _Méthodologie de
validation_.

## Plans Forge produits

| # | Titre | Lots | Validation `validate_or_split` | Notes |
|---|-------|------|--------------------------------|-------|
| 1 | TBD   | TBD  | TBD (OK / erreurs)              | TBD   |

> Si plus d'un plan a été généré : ajouter une ligne par plan. Ne pas
> coller le contenu intégral des plans ici — ils restent dans le
> workspace Squad.

## Enseignements clés

- TBD — observation marquante 1 (qualité, surprises, échecs).
- TBD — observation marquante 2.
- TBD — observation marquante 3.

## Ajustements décidés

> Chaque entrée doit être reportée dans `docs/TUNING.md` (table
> _Décisions actives_) avec un lien vers le commit/PR.

- TBD — décision 1 + raison + référence commit.
- TBD — décision 2.

## Limites connues de cette validation

- TBD — par exemple : "benchmark cappé sur 3 axes par budget normal,
  donc certains concurrents potentiels non couverts".
