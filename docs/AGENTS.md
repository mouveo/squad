# Agents produit orchestrés par Squad

> Ce document décrit les **agents produit** que Squad fait collaborer
> phase après phase pour instruire un sujet. C'est différent de
> `AGENTS.md` à la racine, qui décrit les rôles internes pris par
> Claude quand il *travaille sur Squad lui-même*.
>
> **Migration v2** : la composition est passée de 10 agents v1 à 3
> agents runtime (PM, UX, Architect) plus le service Research. Voir
> `docs/v1-archive.md` pour l'inventaire des agents retirés et la
> commande de récupération via le tag `squad-v1-final`.

Les définitions sources vivent dans `agents/*.md` — un markdown par
agent, lu directement par `squad/executor.py` et injecté dans le prompt
Claude au moment de l'appel.

## Vue d'ensemble (v2)

| Agent | Type | Phase(s) | Pose des questions ? |
|-------|------|----------|----------------------|
| PM (Product Manager) | principal | cadrage, synthèse | Oui — seul interlocuteur utilisateur |
| UX Designer          | secondaire | état des lieux, conception | Non |
| Architect            | secondaire | conception, challenge | Non |
| Research (intégré)   | service | benchmark | Non |

L'agent _Research_ n'est pas un fichier markdown : il est implémenté
directement dans `squad/research.py` (budget déterministe, axes,
template de prompt, injection du skill `deep-research`).

`agents/security.md` reste sur le disque comme **source temporaire**
pour la conversion en checklist de challenge intégrée — il n'est plus
exécuté dans le pipeline runtime. Voir le marqueur
`TODO(squad-v2-lot-2)` en tête du fichier.

## Types d'agents

- **principal** : pilote la phase, peut bloquer le pipeline. PM est le
  seul agent autorisé à interroger l'utilisateur.
- **secondaire** : produit un point de vue spécialisé. Une défaillance
  est loguée mais n'arrête pas la phase.
- **service** : code Python, pas un markdown — Research aujourd'hui.

## Mapping phase → agents

```
1. cadrage           → PM
2. etat_des_lieux    → UX
3. benchmark         → Research (service)
4. conception        → UX, Architect
5. challenge         → Architect
6. synthese          → PM
```

Le mapping authoritative vit dans `squad/phase_config.py` (table
`PHASE_CONFIGS`). Pour chaque agent, `phase_config.py` définit aussi :

- la criticité (`is_critical_agent`),
- la politique de retry (`RetryPolicy`),
- les conditions de skip (`SkipPolicy`, ex. `should_skip_phase` saute
  le benchmark en mode `light`).

Le challenge en v2 ne tourne qu'avec Architect ; il peut produire un
contrat `blockers` qui déclenche **un seul** retour automatique en
conception (`squad/recovery.py:can_retry_conception`).

## Cycle de vie d'un agent dans le pipeline

1. `squad/pipeline.py` itère `iter_phases()`.
2. Pour chaque agent de la phase, `squad/executor.py` :
   - charge la définition markdown (`agents/{name}.md`),
   - construit le prompt cumulatif via `squad/context_builder.py`,
   - appelle Claude CLI (`claude --print --output-format stream-json`),
   - parse le NDJSON et concatène les segments `type: "text"`.
3. Le résultat est persisté dans `phase_outputs` (DB) avec son
   `attempt`, et un fichier dans le workspace de session.
4. Si l'agent retourne un contrat structuré (PM cadrage avec questions,
   PM synthèse avec décisions, Architect challenge avec blockers), il
   est parsé par `squad/phase_contracts.py`.

## Ajouter un nouvel agent

1. Créer `agents/{kebab-name}.md` avec les sections : Identité,
   Mission, Réflexes, Questions clés, Format de sortie.
2. L'enregistrer dans `squad/phase_config.py` (phase, type, retry,
   critical).
3. Si l'agent produit un contrat structuré, ajouter le parseur dans
   `squad/phase_contracts.py`.
4. Tester via `tests/test_agents.py` (présence des sections
   obligatoires) et `tests/test_phase_contracts.py` si parseur.

## Conventions des markdowns d'agent

- Nom de fichier : `kebab-case.md` (ex. `architect.md`).
- Sections obligatoires : `# Agent: <Nom>`, `## Identité`, `## Mission`,
  `## Réflexes`, `## Questions clés`.
- Identité doit lister explicitement la / les phases d'intervention,
  le type, et si l'agent peut interroger l'utilisateur.
- Les "Questions clés" servent de référence interne — elles ne sont
  pas envoyées comme telles à l'utilisateur ; PM les pose après les
  avoir contextualisées.
