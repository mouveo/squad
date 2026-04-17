# Exemple — CRM Sitavista (validation manuelle)

> **Lot 5 du Plan 3.** Validation manuelle hors CI de Squad sur un projet
> réel externe (`~/Developer/sitavista`). Ce dossier contient
> l'**archive nettoyée** d'une session : pas les logs bruts, pas les
> plans bruts, pas les données métier.

## Pourquoi cet exemple existe

Squad ne se valide pas seulement par les tests unitaires : il faut
vérifier sur un vrai projet que les agents produisent des questions
utiles, un benchmark plausible, des plans atomiques et une synthèse
actionnable. Cet exemple sert de référence vivante pour comparer les
sessions futures, et pour repérer les régressions perceptibles
uniquement à l'usage.

## Comment lancer la validation

```bash
squad run ~/Developer/sitavista \
  "améliorer le CRM : gestion des leads, pipeline de vente, scoring automatique, relances"
```

L'exécution est interactive en mode `approval` : Squad pause sur les
questions de cadrage, l'opérateur répond inline, puis valide les plans
générés. Compter ~10–20 minutes de temps mur et une consommation Claude
non négligeable (modèles Opus + WebSearch/WebFetch).

## Ce qui est versionné dans ce dossier

- `summary.md` — résumé décisionnel : durée, coût estimé, note par
  phase, plans validés, enseignements clés. Mis à jour à chaque
  validation.

C'est tout. Pas de logs bruts, pas de plans bruts, pas de transcripts
des questions/réponses — ces artefacts vivent dans
`{projet}/.squad/sessions/{id}/` et ne quittent pas la machine de
l'opérateur.

## Ce qui n'est PAS versionné (et pourquoi)

| Artefact | Raison |
|----------|--------|
| Logs NDJSON bruts du Claude CLI | Verbeux, non triés, contiennent des extraits de prompt. |
| Plans Forge complets générés    | Trop longs, datés ; le résumé suffit comme repère qualitatif. |
| Réponses utilisateur aux questions | Peuvent contenir des éléments produit non publics. |
| Transcripts des phases          | Idem ; remplacés par la note de 1–3 lignes du `summary.md`. |
| Fichiers de la base SQLite      | État local, non comparable d'une exécution à l'autre. |
| Webhooks, tokens, clefs API     | Aucune raison de les fuiter. |

## Comment ajouter une nouvelle validation

1. Lancer la commande ci-dessus depuis la machine de l'opérateur.
2. Une fois la session terminée (statut `approved` ou `done`), remplir
   ou mettre à jour `summary.md` avec les sections du squelette :
   métadonnées, note par phase, plans validés, enseignements.
3. Reporter chaque ajustement décidé dans `docs/TUNING.md` (table
   _Décisions actives_) avec un lien vers le commit/PR qui
   l'implémente.
4. Commit en un seul lot : `chore(examples): record sitavista
   validation YYYY-MM-DD`.

## État courant

> La première validation est attendue de l'opérateur (LOT 5 — Plan 3,
> 2026-04-17). `summary.md` contient pour l'instant un squelette ; il
> sera renseigné après l'exécution réelle.
