# Squad — Plan 8/N : Dashboard local Streamlit (phase B de l'interface humaine)

> La phase A de l'interface humaine (Slack bidirectionnel, plan 4) est
> mergée. La phase B — un dashboard local Streamlit — avait été laissée
> pour plus tard. On la livre ici.
>
> Objectif : un `squad dashboard` qui démarre un serveur Streamlit
> local sur `http://localhost:8501`, lit la DB SQLite + les workspaces
> de session directement (pas de couche API intermédiaire), et donne
> au Product Owner une vue riche complémentaire à Slack :
>
> * Liste des sessions actives et historiques, filtrables par projet
>   et statut, triées par date.
> * Détail d'une session : timeline des phases avec durée, outputs
>   markdown inline, questions ouvertes, angles ideation, attachments,
>   plans générés.
> * Vue "diff" des plans avant approbation (le `.md` complet affiché
>   en lecture + boutons approve/reject branchés sur les mêmes services
>   métier que les boutons Slack du plan 6, donc zéro duplication de
>   logique).
>
> Tout est en local (SQLite + workspace filesystem), pas de serveur
> distant, pas d'auth. Le dashboard lit uniquement — les écritures
> (approve/reject) passent par les mêmes fonctions que les handlers
> Slack (`approve_and_submit`, `reject_session`, etc.) pour garantir
> la cohérence.
>
> Prérequis : plans 1 à 7 mergés.

---

## LOT 1 — Dépendance optionnelle streamlit + commande `squad dashboard`

Ajoute `streamlit>=1.35,<2.0` dans `pyproject.toml` sous une nouvelle
clé `[project.optional-dependencies] dashboard`. Installation :
`pip install -e ".[dashboard]"`. Aucun impact sur les utilisateurs
CLI-only.

Nouvelle commande Click `squad dashboard` dans `squad/cli.py` avec
option `--port` (défaut 8501) et `--host` (défaut `127.0.0.1`). Elle
lance Streamlit via `streamlit.web.cli.main_run` ou via subprocess sur
le fichier `squad/dashboard/app.py`. Import local (dans le corps de la
commande) pour que l'import global de `squad.cli` ne dépende pas de
Streamlit.

Arborescence cible :

```
squad/dashboard/
  __init__.py
  app.py          # entrée Streamlit, routing simple par query params
  sessions.py     # page liste
  session_detail.py
  plans_review.py
  data.py         # adaptateurs DB -> dict pour les pages
```

Crée le dossier et un `app.py` minimal affichant juste "Squad
Dashboard" + le nombre total de sessions pour valider le câblage.

**Success criteria**:
- `pip install -e ".[dashboard]"` installe `streamlit>=1.35,<2.0` et ses deps
- `squad dashboard --port 9999` démarre Streamlit sur le port 9999 sans erreur
- `squad dashboard` sans option démarre sur 8501
- La page racine affiche "Squad Dashboard" et un total de sessions lu depuis la DB
- L'erreur d'import streamlit (si extra non installé) produit un message CLI clair "Dashboard extra not installed — run `pip install -e \".[dashboard]\"` first."
- Nouveau test `tests/test_dashboard.py::test_import_guard` vérifie le ClickException quand streamlit n'est pas importable (mock de l'import)

**Files**: `pyproject.toml`, `squad/cli.py`, `squad/dashboard/__init__.py`, `squad/dashboard/app.py`, `squad/dashboard/data.py`, `tests/test_dashboard.py`

---

## LOT 2 — Page liste des sessions : filtres, statuts, timing

Dans `squad/dashboard/sessions.py`, une fonction `render_sessions_page()`
affiche un tableau Streamlit des sessions avec les colonnes :

* ID court (8 premiers chars, cliquable vers le détail)
* Titre (tronqué 60 chars)
* Projet (basename)
* Statut (pastille colorée : bleu working, orange interviewing, vert
  review/approved, rouge failed)
* Phase courante
* Créée le
* Âge (humanisé : "il y a 2 min", "il y a 3 h", etc.)

Filtres dans une sidebar Streamlit :

* Statut (multi-select)
* Projet (select avec valeurs tirées de la DB distinct)
* "Uniquement actives" (cache approved/failed quand coché)
* Tri : par `created_at desc` (défaut) ou `updated_at desc`

Clic sur un ID → redirige vers `?page=session&id=<uuid>` (routing par
query params géré dans `app.py`).

`squad/dashboard/data.py` expose `list_sessions_for_dashboard(
filters) -> list[SessionRow]` où `SessionRow` est une dataclass avec
les champs affichés (pas la Session complète, pour éviter de trimballer
des JSON lourds).

**Success criteria**:
- La page `Sessions` affiche toutes les sessions en DB sous forme de tableau Streamlit
- Les filtres (statut, projet, actives-only) filtrent réellement les lignes affichées
- L'âge est rendu en français ("il y a X min/h/j")
- Les pastilles de statut ont des couleurs distinctes et accessibles (contraste AA)
- Clic sur un ID met à jour les query params vers `?page=session&id=…`
- Tests `test_dashboard.py::test_list_sessions_filters_by_status` et `::test_humanize_age` couvrent la logique pure (sans Streamlit, juste data.py)

**Files**: `squad/dashboard/sessions.py`, `squad/dashboard/data.py`, `squad/dashboard/app.py`, `tests/test_dashboard.py`

**Depends on**: LOT 1

---

## LOT 3 — Détail session : timeline des phases + outputs inline

`squad/dashboard/session_detail.py` affiche une session en profondeur :

* Header : titre, projet, statut, mode, research_depth, input_richness
* Idée (collapsible, markdown rendu)
* Contexte projet (collapsible, markdown)
* Timeline des phases : 7 blocs (cadrage, etat_des_lieux, ideation,
  benchmark, conception, challenge, synthese), chacun avec :
  * Statut visuel (done/running/pending/skipped/failed)
  * Timestamp de démarrage et durée
  * Outputs agents de la phase en onglets (un onglet par agent)
  * Markdown rendu avec highlight syntax pour les blocs code

* Panneau "Angles ideation" : cards pour chaque angle, avec titre,
  segment, value prop, approach, flag "Sélectionné" sur celui retenu
* Panneau "Attachments" : liste des fichiers joints (nom, taille,
  mime, lien vers le fichier local)
* Panneau "Questions pendantes" (si status=interviewing) : pour info
  uniquement, pas de bouton répondre (la réponse passe par Slack)

Les données viennent de `data.py::get_session_detail(session_id) ->
SessionDetail` qui agrège en un seul appel DB+filesystem.

**Success criteria**:
- La page `Session` affiche le header, l'idée, et les 7 phases
- Une phase `done` affiche ses outputs par agent en onglets ; une phase `pending` reste repliée
- Les angles ideation sont rendus en cards distinctes, l'angle sélectionné a un badge visuel
- Les attachments listent tous les fichiers présents dans `{workspace}/attachments/`
- Navigation : un bouton "Retour à la liste" dans la sidebar revient sur `?page=sessions`
- Test `test_dashboard.py::test_get_session_detail_aggregates_phases_outputs_angles` avec un workspace tmp + DB tmp peuplés

**Files**: `squad/dashboard/session_detail.py`, `squad/dashboard/data.py`, `squad/dashboard/app.py`, `tests/test_dashboard.py`

**Depends on**: LOT 2

---

## LOT 4 — Review des plans + actions approve / reject

`squad/dashboard/plans_review.py` affiche la vue review pour une
session en status `review` :

* Pour chaque plan :
  * Titre + nombre de lots
  * Fichiers touchés par lot (visualisation compacte)
  * Markdown complet du plan dans un `st.expander`
  * Boutons `Approuver` et `Rejeter`

* Clic sur `Approuver` → appelle `squad.forge_bridge.approve_and_submit(
session_id, db_path)` (la même fonction que le bouton Slack plan 4
LOT 5 et la commande CLI `squad approve`). Affiche le résultat dans un
`st.toast` : "Plan soumis à la queue Forge" ou erreur.

* Clic sur `Rejeter` ouvre un modal (input texte raison) puis appelle
`reject_session(session_id, reason, db_path)` (fonction existante
utilisée par Slack). Affiche un toast de confirmation.

Garde la cohérence : pas de chemin parallèle, les deux actions
réutilisent exactement les services métier de Slack. Une session
approuvée via le dashboard est indistinguable d'une approuvée via
Slack ou CLI.

**Success criteria**:
- La page `Plans` affiche tous les plans générés pour une session en status `review`
- Le bouton `Approuver` appelle `approve_and_submit` (mocké dans les tests) et affiche un toast
- Le bouton `Rejeter` ouvre un modal texte, collecte la raison, appelle `reject_session`
- Une session déjà `approved` ou `failed` désactive les boutons et affiche son état final
- Les plans Forge sont validés par `forge_format.validate_plan` côté dashboard aussi, avec un badge "invalid" si format cassé
- Tests `test_dashboard.py::test_approve_plan_calls_forge_bridge`, `::test_reject_plan_calls_reject_session`, `::test_disabled_buttons_on_terminal_status`

**Files**: `squad/dashboard/plans_review.py`, `squad/dashboard/app.py`, `squad/dashboard/data.py`, `tests/test_dashboard.py`

**Depends on**: LOT 3

---

## LOT 5 — Routage par query params + navigation cohérente

Implémente dans `squad/dashboard/app.py` un routage minimal basé sur
les query params Streamlit (`st.query_params`) :

* `?page=sessions` (défaut) → liste
* `?page=session&id=<uuid>` → détail session
* `?page=plans&id=<uuid>` → review plans (si status=review)

La sidebar contient : logo Squad (ASCII ou texte stylé), lien "Toutes
les sessions", compteur "Sessions actives : N", horloge de last
refresh, bouton "Rafraîchir" (force rerun).

Gère proprement les cas d'erreur : session id inconnu → page d'erreur
avec retour vers liste ; page inconnue → redirect vers `sessions`.

Ajoute une section docs dans `docs/ARCHITECTURE.md` (ou crée
`docs/DASHBOARD.md`) expliquant : comment lancer, architecture
(adapters `data.py` lisent DB + FS, pages Streamlit pures de
rendering, actions passent par services métier partagés avec Slack),
et comment ajouter une nouvelle page.

**Success criteria**:
- La navigation entre les pages (liste → détail → plans → retour) fonctionne via clics sans rechargement complet
- Un `?page=session&id=deadbeef` invalide affiche "Session introuvable" + bouton retour
- Un `?page=bogus` redirige vers `?page=sessions`
- La sidebar affiche le compteur de sessions actives et il se met à jour au `Rafraîchir`
- `docs/DASHBOARD.md` existe et décrit les 3 pages + comment ajouter la suivante
- `pytest -m "not integration"` reste 100% pass

**Files**: `squad/dashboard/app.py`, `squad/dashboard/data.py`, `docs/DASHBOARD.md`, `tests/test_dashboard.py`
