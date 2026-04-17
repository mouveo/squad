---
name: deep-research
description: Structured multi-axis benchmark research with sourced findings, distinguishing facts from hypotheses, for product/design pipelines.
---

# Deep Research Protocol

Use this protocol whenever you must produce a benchmark or competitive
research report for a product idea. The output must be **decision-ready**:
sourced, structured and honest about what is known versus inferred.

## Quand l'utiliser

- Benchmark concurrentiel d'un nouveau produit ou module.
- Recherche d'antécédents techniques (patterns éprouvés, retours d'expérience).
- Cartographie de pain points utilisateurs, d'attentes marché, ou de
  contraintes réglementaires sur un sujet précis.

## Protocole

1. **Cadrer les axes.** Lister 3 à 5 axes distincts à couvrir (le moteur
   appelant fournit la liste). Ne pas en inventer d'autres.
2. **Collecter par axe.** Pour chaque axe, faire 2 à 4 recherches Web
   ciblées (`WebSearch`) puis fetcher les sources les plus pertinentes
   (`WebFetch`) pour vérifier les affirmations clés. Ignorer les sources
   non datées, anonymes ou clairement promotionnelles si une alternative
   existe.
3. **Trier les concurrents.** Identifier 3 à 8 acteurs représentatifs.
   Pour chacun : positionnement réel (pas la promesse marketing),
   forces démontrables, limites observées.
4. **Distinguer faits et hypothèses.** Tout chiffre, fonctionnalité ou
   prix cité doit être attaché à une URL vérifiable. Si une affirmation
   ne peut pas être sourcée, soit elle est marquée explicitement comme
   hypothèse, soit elle est retirée.
5. **Synthétiser.** Produire un résumé exécutif décisionnel (3 à 5
   bullets) qui pointe les implications pour le produit, pas les
   généralités du marché.

## Règles de sourcing

- Citer uniquement des URLs réelles et joignables. Si vous n'avez pas
  pu charger la page, ne la citez pas.
- Préférer sources primaires (site officiel, doc technique, post
  d'ingénierie) aux agrégateurs (annuaires, comparateurs SEO).
- Dater les références dès qu'une date est disponible.
- Une source = une ligne de bullet avec un libellé court.

## Format de rendu

Produire un seul document markdown avec exactement ces sections :

```
# Benchmark

## Résumé exécutif
- 3 à 5 bullets, orientés décision produit.

## Concurrents
| Acteur | Positionnement | Forces | Limites |
|--------|----------------|--------|---------|
| ...    | ...            | ...    | ...     |

## Analyse par axe
### {axe 1}
Findings + URLs.

### {axe 2}
...

## Sources
- https://... — libellé court
- https://... — libellé court
```

## Garde-fous

- Ne pas inventer de chiffres, prix, ni noms de produits.
- Ne pas dépasser le budget de caractères imposé par l'appelant.
- Ne pas spéculer hors des axes fournis.
- Si une recherche échoue (timeout, page inaccessible), poursuivre les
  autres axes au lieu de bloquer.
