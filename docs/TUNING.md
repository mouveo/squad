# Squad — Notes de tuning

> Ce document collecte les ajustements décidés à partir des **validations
> manuelles hors CI** de Squad sur des projets réels. Les exécutions
> elles-mêmes (durée, coût, observations détaillées) vivent dans
> `examples/{nom-du-projet}/`. Ce fichier ne garde que le _verdict_ : ce
> qu'on change dans le code ou la configuration, et pourquoi.

## Méthodologie de validation

Chaque session de validation manuelle est jugée selon la grille suivante,
phase par phase. C'est volontairement court pour rester actionnable — pas
une spec QA exhaustive.

| Phase           | Critère minimum |
|-----------------|-----------------|
| Cadrage         | Questions utiles, non redondantes, adaptées au sujet. |
| État des lieux  | Lecture du code existant, identification correcte des briques en place. |
| Benchmark       | Concurrents plausibles, sources citées, distinction faits / hypothèses. |
| Conception      | Propositions concrètes et cohérentes avec le contexte produit. |
| Challenge       | Risques réels identifiés sur sécurité, delivery, dette. |
| Synthèse        | Résumé décisionnel actionnable. |
| Plans Forge     | Lots atomiques, dépendances claires, validation `validate_or_split` réussie. |

Pour chaque session, on consigne aussi :

- la commande exécutée (`squad run …`),
- la durée totale et le coût estimé,
- une note brute par phase (1–3 lignes),
- la liste des décisions de tuning qui en découlent (entrées dans la
  table ci-dessous).

Voir `examples/crm-sitavista/README.md` pour le format des artefacts
archivés.

## Format d'une décision de tuning

Une entrée tient en 4 champs : `Date`, `Phase`, `Décision`, `Raison`.
Le code/PR qui l'implémente est référencé dans la colonne `Suivi`.

| Date       | Phase | Décision | Raison | Suivi |
|------------|-------|----------|--------|-------|
| _AAAA-MM-JJ_ | _phase_ | _ce qu'on change_ | _ce qu'on a observé_ | _commit / PR_ |

## Décisions actives

> _Aucune décision enregistrée pour l'instant. La première validation
> manuelle (LOT 5 du Plan 3 — Sitavista, 2026-04-17) doit être exécutée
> par l'opérateur et ses ajustements ajoutés ici._

| Date | Phase | Décision | Raison | Suivi |
|------|-------|----------|--------|-------|
| —    | —     | —        | —      | —     |

## Décisions archivées

> Vider de cette table une décision quand elle est devenue obsolète
> (refactor, suppression d'une option, changement de modèle). Conserver
> trace dans le log Git.
