# Squad — Plan 4/N : Interface Slack interactive (Phase A)

> Squad dispose aujourd'hui d'une CLI et d'un webhook Slack sortant unidirectionnel.
> Ce plan ajoute une véritable interface Slack bidirectionnelle : le Product Owner
> peut créer des sessions, uploader des fichiers, répondre aux questions des agents
> et approuver les plans Forge — entièrement depuis Slack (desktop ou mobile).
>
> Architecture : Slack App en Socket Mode via `slack-bolt`, pas de tunnel requis.
> Toute la logique métier vit dans des services Python réutilisables (`slack_service`,
> `attachment_service`) — la phase B (dashboard Streamlit) réutilisera ces services
> sans duplication.
>
> Prérequis : plans 1, 2 et 3 complets (CLI, pipeline, intégration Forge fonctionnels).

---

## LOT 1 — Squelette Bolt + commande `squad serve` + `/squad new` end-to-end

Pose les fondations techniques : dépendance optionnelle `slack-bolt`, commande CLI
`squad serve` qui démarre l'app Slack en Socket Mode, et handler minimal pour le
slash command `/squad new <idée>` qui crée une session en DB et répond dans un
thread dédié. Le `notifier.py` actuel (webhook sortant) est supprimé — les
notifications passent désormais exclusivement par `slack_service.post_*` via
l'API Slack bidirectionnelle.

Migration DB additive (pattern `ensure_schema` existant dans `db.py`) : ajout de
trois colonnes `slack_channel`, `slack_thread_ts`, `slack_user_id` sur la table
`sessions`. La configuration Slack (tokens, allowlist d'user_ids) est ajoutée au
`DEFAULT_CONFIG_YAML` dans `config.py` avec interpolation `${SQUAD_SLACK_*}`.

Les fichiers `slack_app.py`, `slack_handlers.py` et `slack_service.py` sont
découpés par responsabilité : `slack_app.py` initialise l'App Bolt et enregistre
les handlers, `slack_handlers.py` parse les événements Slack et délègue à
`slack_service.py` qui porte la logique métier Slack-agnostique.

Le pipeline Squad étant synchrone et long (~15 minutes), il est lancé dans un
`ThreadPoolExecutor` par le handler pour ne pas bloquer la boucle asyncio de
Bolt. L'exécuteur est instancié au démarrage de `squad serve` et fermé
proprement à l'arrêt.

**Success criteria**:
- `pip install -e ".[slack]"` installe `slack-bolt>=1.20,<2.0` sans conflit
- `squad serve` démarre et se connecte à Slack en Socket Mode avec les tokens
  `SQUAD_SLACK_BOT_TOKEN` et `SQUAD_SLACK_APP_TOKEN` (sinon erreur claire)
- `/squad new <idée>` dans un channel crée une session en DB (status `draft`),
  démarre le pipeline dans un thread d'exécution, et poste un message de
  confirmation en thread contenant session_id et titre détecté
- Les colonnes `slack_channel`, `slack_thread_ts`, `slack_user_id` sont
  correctement peuplées sur la nouvelle session
- `squad status` CLI liste la session créée depuis Slack
- `squad/notifier.py` et ses tests sont supprimés ; aucun import résiduel
- `tests/test_slack_service.py::test_create_session_from_slack` passe avec
  un client Slack mocké

**Files**: `pyproject.toml`, `squad/slack_app.py`, `squad/slack_handlers.py`, `squad/slack_service.py`, `squad/cli.py`, `squad/db.py`, `squad/models.py`, `squad/config.py`, `squad/constants.py`, `squad/notifier.py`, `tests/test_slack_service.py`, `tests/test_slack_handlers.py`, `tests/test_notifier.py`

---

## LOT 2 — Suivi live des phases dans le thread Slack

Ajoute des notifications de progression postées dans le thread Slack de la
session à chaque transition de phase du pipeline (`cadrage` démarré,
`benchmark` terminé, etc.). L'objectif est que le PO voie en temps réel
l'avancement sans taper `squad status`.

Le `pipeline.py` accepte un paramètre optionnel `on_phase_transition:
Callable[[Session, str, str], None]` appelé à chaque changement de phase.
Le `slack_handlers.py` passe un callback qui appelle
`slack_service.post_phase_update`. La CLI classique (`squad start`, `squad run`)
ne passe aucun callback — aucune régression comportementale.

Chaque message inclut la phase courante, l'horodatage UTC, la durée écoulée
depuis le démarrage de la session, et le statut attendu à la fin de la phase.
Un message récapitulatif est posté à l'entrée en statut `review`, `approved`
ou `failed`.

**Success criteria**:
- `pipeline.run_pipeline` accepte un callback `on_phase_transition` optionnel,
  rétrocompatible (tous les tests existants passent sans modification)
- Chaque transition vers une phase `working/<phase>` poste un message dans le
  thread de la session avec phase, horodatage, durée cumulée
- L'entrée en `review` poste un récapitulatif (nombre de plans, durée totale)
- L'entrée en `failed` poste la raison d'échec
- `tests/test_slack_service.py::test_post_phase_update` couvre les trois cas
  (working, review, failed)

**Files**: `squad/slack_service.py`, `squad/slack_handlers.py`, `squad/pipeline.py`, `tests/test_slack_service.py`, `tests/test_pipeline.py`

**Depends on**: LOT 1

---

## LOT 3 — Upload de fichiers dans le thread → contexte des agents

Permet au PO de joindre des fichiers (specs PDF, screenshots, exports CSV,
notes markdown) dans le thread Slack d'une session. Les fichiers sont
téléchargés via l'API Slack Files (bot token + URL privée), stockés dans
`{workspace}/attachments/` et injectés dans le contexte cumulatif envoyé aux
agents.

Un nouveau module `attachment_service.py` porte la logique de téléchargement
(`download_slack_file(url, token) -> bytes`), de stockage (`store_attachment`)
et de listing (`list_attachments -> list[AttachmentMeta]`). Une dataclass
`AttachmentMeta(session_id, filename, size, mime_type, stored_path, created_at)`
vit dans `models.py`.

Le `context_builder.build_cumulative_context` reçoit une nouvelle section
`## Fichiers joints` entre la section Q&A et les outputs de phases. Les
fichiers texte (md, txt, csv) sont inlinés tronqués à 8000 caractères. Les
fichiers binaires (pdf, png, jpg) sont mentionnés par nom et mime_type mais
pas inlinés — un futur lot pourra ajouter OCR/parsing.

Limites : 10 Mo par fichier, 50 Mo cumulés par session, extensions autorisées
configurables via `config.yaml` (`slack.attachments.allowed_extensions`).
Rejet avec message d'erreur dans le thread si dépassement.

**Success criteria**:
- Drag-drop d'un fichier `.md` ou `.pdf` dans un thread de session le
  télécharge dans `{workspace}/attachments/`
- Le handler Slack `file_shared` filtre les événements hors-thread-de-session
  (lookup via `get_session_by_thread`) et ignore silencieusement les autres
- `build_cumulative_context` inclut une section `## Fichiers joints` listant
  chaque fichier (nom, taille, mime_type) et inline le contenu des fichiers
  texte tronqué à 8000 chars
- Fichier > 10 Mo : message d'erreur posté dans le thread, aucun fichier
  stocké, pas de crash
- Extension non autorisée : message d'erreur, pas de stockage
- `tests/test_attachment_service.py` couvre download (httpx mocké), store,
  list, limites de taille et d'extension
- `tests/test_context_builder.py::test_format_attachments` passe

**Files**: `squad/attachment_service.py`, `squad/workspace.py`, `squad/context_builder.py`, `squad/slack_handlers.py`, `squad/slack_service.py`, `squad/models.py`, `squad/config.py`, `squad/db.py`, `tests/test_attachment_service.py`, `tests/test_context_builder.py`, `tests/test_workspace.py`

**Depends on**: LOT 1

---

## LOT 4 — Questions PM postées en thread + réponses → reprise pipeline

Rend le Q&A interactif dans Slack. Quand le pipeline pause en `interviewing`,
chaque question pending est postée comme message séparé dans le thread Slack
(avec `question_id` en block metadata). Le PO répond en thread au message de
la question ; le handler `message` matche le `thread_ts` de la réponse avec
`slack_message_ts` stocké sur la question, persiste la réponse via
`answer_question`, et si toutes les questions d'un agent sont répondues,
appelle `resume_pipeline`.

Migration DB additive : colonne `slack_message_ts` sur la table `questions`.
Nouveau helper `get_session_by_thread(channel, thread_ts)` dans `db.py`.

La logique de reprise respecte scrupuleusement `recovery.py` existant —
aucun chemin parallèle : le handler appelle les mêmes fonctions que
`squad answer` + `squad resume`. Une double réponse à la même question
écrase la première (comportement identique à la CLI).

**Success criteria**:
- À l'entrée en `interviewing`, chaque question pending est postée en message
  séparé dans le thread ; leur `slack_message_ts` est persisté
- Une réponse threadée à une question déclenche `answer_question` puis
  `sync_pending_questions` ; le thread reçoit un accusé de réception
- Quand la dernière question est répondue, `resume_pipeline` est appelé et
  le pipeline reprend sur la phase suivante
- Réponses multiples à la même question : la dernière gagne, pas de crash,
  pas de messages doublons
- Réponse dans un thread hors-session : ignorée silencieusement
- `tests/test_slack_handlers.py::test_thread_reply_triggers_resume` passe
- `tests/test_slack_handlers.py::test_thread_reply_ignored_outside_session` passe

**Files**: `squad/slack_handlers.py`, `squad/slack_service.py`, `squad/db.py`, `squad/models.py`, `squad/pipeline.py`, `squad/recovery.py`, `tests/test_slack_handlers.py`, `tests/test_slack_service.py`, `tests/test_db.py`

**Depends on**: LOT 1

---

## LOT 5 — Boutons Approve / Reject sur les plans + soumission Forge

Ferme la boucle humaine. À l'entrée en `review`, chaque plan généré est posté
dans le thread : un résumé de 3 à 5 lignes (titre, nombre de lots, fichiers
principaux impactés) suivi du fichier `.md` complet uploadé via l'API Slack
Files, puis deux boutons Block Kit `Approuver` / `Rejeter`.

Le clic sur Approuver appelle `update_session_status(approved)` puis
`forge_bridge.submit_session_to_forge` ; un message de confirmation avec
nombre de plans envoyés à la queue Forge est posté. Le clic sur Rejeter
appelle `update_session_status(failed)` et poste la raison (demandée en
champ texte via `views_open` modal).

Idempotence : après le premier clic, le message est mis à jour via
`chat_update` pour désactiver les boutons et afficher "Approuvé le <date>"
ou "Rejeté le <date>". Un guard en tête du handler vérifie le status de la
session pour ignorer les clics sur un plan déjà traité.

**Success criteria**:
- À l'entrée en `review`, chaque plan est posté dans le thread : résumé en
  3-5 lignes + `.md` complet uploadé comme fichier Slack
- Deux boutons `Approuver` / `Rejeter` accompagnent chaque plan avec
  `action_id` encodant `approve:{session_id}` / `reject:{session_id}`
- Clic Approuver : `update_session_status(approved)` + soumission Forge +
  message de confirmation avec ids de queue Forge
- Clic Rejeter : modal pour saisir une raison, puis
  `update_session_status(failed)` + raison postée
- Après clic, message mis à jour (boutons désactivés + label horodaté)
- Double-clic : second clic ignoré, aucun état incohérent
- `squad status` CLI reflète le changement de statut
- `tests/test_slack_handlers.py::test_approve_action_submits_to_forge` passe
  avec `forge_bridge` mocké
- `tests/test_slack_handlers.py::test_approve_action_idempotent` passe

**Files**: `squad/slack_handlers.py`, `squad/slack_service.py`, `squad/forge_bridge.py`, `squad/db.py`, `tests/test_slack_handlers.py`, `tests/test_slack_service.py`

**Depends on**: LOT 1, LOT 4
