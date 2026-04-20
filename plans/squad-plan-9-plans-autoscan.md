# Squad — Plan 9/N : auto-scan `{projet}/plans/<subject>/*.md` au démarrage de session

> Le workflow constaté en usage réel :
>
> 1. L'utilisateur prépare un brief + des documents d'appui dans
>    `~/Developer/<projet>/plans/<subject>/` (ex:
>    `ressort/plans/whaou/prompt.md`, `produit-actuel.md`,
>    `contraintes-et-bornes.md`).
> 2. Il poste `/squad new "<idée mentionnant le subject>"` et drag-drop
>    les 2-3 fichiers dans le composer Slack.
> 3. Slack envoie la slash command puis les file_shared events, avec un
>    léger décalage. Avec le fix `6d31186` le fallback auto-attach par
>    recency kick in, donc les fichiers arrivent — mais 30 à 60 s après
>    le démarrage de la phase cadrage.
>
> Conséquence : la phase cadrage (PM) tourne **sans** les documents. Le
> PM reformule l'idée sans voir contraintes + produit-actuel. Les
> phases suivantes ont les fichiers, mais le cadrage initial — qui
> pilote tout — est pauvre.
>
> Ce plan introduit un **scan automatique** : à la création d'une
> session, Squad regarde si l'idée mentionne un dossier sous
> `{project_path}/plans/` et, si oui, importe tous les `.md` de ce
> dossier comme s'ils avaient été droppés via Slack. Les fichiers sont
> présents dès la phase cadrage, sans race condition possible.
>
> Entrée CLI et Slack couvertes à l'identique : le scan est déclenché
> dans le chemin partagé de création de session.
>
> Prérequis : plans 1 à 8 mergés. Utilise la logique de tokenisation
> déjà en place dans `discover_project_path` pour l'extraction du
> subject slug.

---

## LOT 1 — `import_local_attachment` dans attachment_service

Ajoute une fonction `import_local_attachment(session_id, src_path,
*, config=None, db_path=None) -> AttachmentMeta` dans
`squad/attachment_service.py`. Elle prend un chemin local
(`pathlib.Path`), valide la taille et l'extension via le même
`validate_attachment` que le chemin Slack, lit le fichier en bytes,
puis stocke avec `store_attachment` en passant `slack_file_id=None`
(c'est un attachment local, pas Slack).

Cette fonction réutilise intégralement la validation existante : si le
fichier dépasse `slack.attachments.max_file_bytes` ou n'est pas dans
les `allowed_extensions`, une `AttachmentError` remonte comme pour un
drop Slack. Taille cumulée par session également enforce.

Aucun changement côté `store_attachment` — il acceptait déjà
`slack_file_id` optionnel.

**Success criteria**:
- `import_local_attachment` lit un `.md` de 1 KB, stocke dans `{workspace}/attachments/`, renvoie un `AttachmentMeta` avec `slack_file_id=None`.
- Rejette un `.exe` avec `AttachmentError("extension non autorisée")`.
- Rejette un fichier > 10 Mo avec `AttachmentError("taille dépassée")`.
- Respecte le plafond `max_total_bytes` quand des attachments Slack existent déjà sur la session.
- Tests dans `tests/test_attachment_service.py::TestImportLocalAttachment` : 4 cas (nominal, extension, taille, plafond cumulé).

**Files**: `squad/attachment_service.py`, `tests/test_attachment_service.py`

---

## LOT 2 — Module `plans_autoscan` : discovery + scan

Nouveau module `squad/plans_autoscan.py` avec deux fonctions pures :

* `discover_plans_subfolder(idea: str, project_path: Path) -> Path | None`
  Tokenise `idea` via le même `re.findall(r"[a-z0-9][a-z0-9\-_]{2,}",
  idea.lower())` que `slack_service.discover_project_path` pour rester
  cohérent. Pour chaque token ≥ 3 chars, vérifie si
  `project_path/plans/<token>/` existe comme dossier. Retourne le
  match le plus long (même règle que discovery de projet pour
  déterminisme). Retourne `None` si `project_path/plans/` n'existe pas
  ou si aucun token ne match.

* `scan_plans_folder(folder: Path, *, max_files: int = 10) -> list[Path]`
  Retourne la liste triée des fichiers `.md`, `.txt`, `.csv` (mêmes
  extensions texte que le scanner Slack par défaut) directement dans
  le dossier (pas récursif — on ne veut pas absorber des
  sous-dossiers comme `done/`). Cap à `max_files=10` pour éviter
  d'aspirer un backlog entier.

Les deux fonctions sont pures (sans I/O autre que lecture du FS pour
tester l'existence), donc testables sans fixture Slack.

**Success criteria**:
- `discover_plans_subfolder("Ajouter le module whaou à ressort", Path("~/Developer/ressort"))` retourne `Path("~/Developer/ressort/plans/whaou")` quand ce dossier existe.
- Retourne `None` si aucun token de l'idée ne correspond à un sous-dossier de `plans/`.
- `scan_plans_folder(folder)` retourne tous les `.md/.txt/.csv` tri alphabétique, cap à 10.
- `scan_plans_folder(folder)` n'entre pas dans les sous-dossiers (test avec `folder/done/old.md` qui doit être ignoré).
- Tests : `tests/test_plans_autoscan.py` couvre les 4 cas (match, no-match, extensions filtrées, pas-de-récursion).

**Files**: `squad/plans_autoscan.py`, `tests/test_plans_autoscan.py`

---

## LOT 3 — Hook dans `create_session_from_slack`

Dans `squad/slack_service.py::create_session_from_slack`, juste après
la création du workspace et avant le return, appelle
`_autoscan_and_import_plans(session, idea, db_path, config)` — une
nouvelle fonction locale au module qui :

1. Si la config `pipeline.project_plans_autoscan` est `false`, no-op
   silencieux (log INFO).
2. Sinon, appelle `discover_plans_subfolder(idea,
   Path(session.project_path))`. Retourne si `None`.
3. Appelle `scan_plans_folder(folder)`. Si vide, log INFO et retourne.
4. Pour chaque fichier, appelle
   `attachment_service.import_local_attachment(session.id, path,
   config=config, db_path=db_path)`. Sur `AttachmentError`, log
   WARNING avec le nom du fichier et continue (un fichier rejeté ne
   doit pas bloquer les autres).
5. Log INFO récapitulatif : `"Auto-scan: %d file(s) imported from %s"`.

Poste un message dans le thread Slack après la création de la session
(si au moins 1 fichier importé) : `":open_file_folder: N fichier(s)
auto-attaché(s) depuis <folder>"` — miroir de la confirmation Slack
habituelle.

**Success criteria**:
- Session Slack créée avec idée contenant `"whaou"` + dossier `ressort/plans/whaou/` peuplé → les 3 `.md` du dossier sont dans `{workspace}/attachments/` avant la première phase.
- Config `pipeline.project_plans_autoscan: false` → aucun import, log INFO explicite.
- Fichier rejeté (extension non autorisée) → log WARNING + les autres fichiers passent.
- Thread Slack reçoit un message `:open_file_folder: N fichier(s)` quand au moins 1 import réussit.
- Test `tests/test_slack_service.py::TestAutoScanPlans` avec un workspace tmp + un dossier de plans peuplé.

**Files**: `squad/slack_service.py`, `squad/config.py`, `tests/test_slack_service.py`

**Depends on**: LOT 1, LOT 2

---

## LOT 4 — Hook dans la CLI (`squad start` / `squad run`)

Dans `squad/cli.py::_create_and_init_session`, appelle le même
`_autoscan_and_import_plans` (exposé publiquement depuis
`squad.slack_service` ou déplacé dans un nouveau module neutre
`squad.session_bootstrap`). Bénéfice : parité stricte entre la CLI
et Slack — l'utilisateur qui lance `squad run` depuis un terminal
obtient le même auto-scan que depuis Slack.

Ajoute une option CLI `--no-plans-autoscan` sur `squad start` et
`squad run` pour désactiver ponctuellement, utile quand on veut
relancer une session sans ré-importer les docs existants.

Met à jour le `click.echo` de démarrage pour afficher
`"Auto-scan : N fichier(s) importé(s) depuis plans/<folder>"` quand
le scan aboutit — visibilité immédiate en terminal.

**Success criteria**:
- `squad start ~/Developer/ressort "Lancer le sujet whaou"` → les `.md` de `~/Developer/ressort/plans/whaou/` sont dans le workspace avant la première phase.
- `squad start ... --no-plans-autoscan` → aucun import, message CLI `"Auto-scan : désactivé"`.
- `squad run` suit la même logique.
- Test `tests/test_cli_commands.py::TestStartAutoScan` avec un project tmp + plans folder peuplé.
- Les tests existants de `test_cli_commands.py` passent sans modification (le scan no-op quand il n'y a pas de dossier correspondant).

**Files**: `squad/cli.py`, `squad/slack_service.py` (ou nouveau `session_bootstrap.py`), `tests/test_cli_commands.py`

**Depends on**: LOT 3

---

## LOT 5 — Config par défaut + docs

Ajoute dans `DEFAULT_CONFIG_YAML` (`squad/config.py`) une clé
commentée :

```yaml
# Pipeline tuning.
pipeline:
  # ...
  # Auto-import every *.md/*.txt/*.csv from {project}/plans/<subject>/
  # when the idea mentions <subject>. Disable with `false` if the
  # project uses `plans/` for unrelated purposes.
  # project_plans_autoscan: true
```

Enrichis trois docs :

1. `docs/FOR_AI_COLLABORATORS.md` — section "Anatomie d'un bon
   brief" : ajouter une option préférée "Place tes documents d'appui
   dans `{projet}/plans/<subject>/` et mentionne `<subject>` dans ton
   idée — Squad importe automatiquement". Souligner que c'est
   préférable au drag-drop Slack quand les docs sont volumineux ou
   nombreux.
2. `docs/TROUBLESHOOTING.md` — nouvelle entrée "Auto-scan n'a pas
   importé mes fichiers" avec checklist : dossier bien sous `plans/`,
   nom du dossier mentionné dans l'idée, extension autorisée, config
   `pipeline.project_plans_autoscan` non désactivée, logs serve.log.
3. `docs/SITAVISTA_TEST.md` — mettre à jour la procédure type pour
   utiliser le workflow "prépare tes docs dans
   `sitavista/plans/<subject>/` puis `/squad new`" en première option,
   Slack attachment en fallback.

**Success criteria**:
- `DEFAULT_CONFIG_YAML` contient la clé `project_plans_autoscan` commentée avec la valeur par défaut `true` et un commentaire explicatif.
- `docs/FOR_AI_COLLABORATORS.md` mentionne le workflow auto-scan en premier dans la section attachments, avec un exemple concret (ressort/plans/whaou).
- `docs/TROUBLESHOOTING.md` a une nouvelle section "Auto-scan silencieux" avec 5 points de diagnostic.
- `docs/SITAVISTA_TEST.md` a sa procédure principale réécrite pour pointer sur `sitavista/plans/<subject>/`.
- Aucune régression fonctionnelle : les tests de config, slack_service et cli_commands passent.

**Files**: `squad/config.py`, `docs/FOR_AI_COLLABORATORS.md`, `docs/TROUBLESHOOTING.md`, `docs/SITAVISTA_TEST.md`

**Depends on**: LOT 3, LOT 4
