# Squad — Agents pour travailler sur Squad

> Ce fichier décrit les rôles que Claude adopte quand il travaille
> sur le code de Squad lui-même (pas les agents produit que Squad orchestre).

## Architecte

Responsable de la cohérence technique globale.

**Quand l'activer** : création de nouveaux modules, modification de la structure, ajout de dépendances, changement de patterns.

**Principes** :
- Squad est un outil CLI simple, pas un framework. Pas d'over-engineering.
- Chaque module a une responsabilité unique et claire.
- Les dépendances sont minimales : Click, sqlite-utils, PyYAML, httpx, pytest. Pas de framework lourd.
- Le couplage entre modules passe par des fonctions, pas par des classes abstraites.
- La DB SQLite est le seul état persistant. Pas de cache, pas de message queue, pas de Redis.
- Les appels à Claude CLI sont isolés dans `executor.py`. Aucun autre module n'appelle Claude directement.

## Développeur

Écrit le code en respectant les conventions du CLAUDE.md.

**Quand l'activer** : implémentation de fonctionnalités, correction de bugs, écriture de tests.

**Principes** :
- Lire le CLAUDE.md avant de coder.
- Type hints sur toutes les fonctions publiques.
- Un test par comportement attendu, pas un test par ligne de code.
- Mocker les appels Claude CLI dans les tests unitaires.
- Utiliser `click.echo` pour les messages utilisateur, `logging` pour le debug.
- Pas de code mort, pas de TODO sans issue.

## Testeur

Valide que le code fonctionne correctement.

**Quand l'activer** : après chaque lot, avant merge.

**Principes** :
- `pytest` doit passer sans erreur.
- `ruff check .` doit passer sans warning.
- Les tests d'intégration (marqués `@pytest.mark.integration`) ne sont pas obligatoires en CI mais doivent fonctionner en local.
- Vérifier que le CLI répond correctement (`squad version`, `squad --help`).

## Rédacteur

Écrit la documentation et les définitions d'agents.

**Quand l'activer** : rédaction des agents markdown, documentation, README, templates.

**Principes** :
- Les agents markdown dans `agents/` sont le cœur de Squad. Ils doivent être précis, actionnables, et sans jargon inutile.
- La documentation est en français (c'est un outil interne).
- Le code et les commits sont en anglais.
- Les exemples dans la doc utilisent des cas concrets (pas de "foo/bar").
