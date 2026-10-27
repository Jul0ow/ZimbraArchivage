# Revue — Optimisation & performance

Périmètre : backend FastAPI (`backend/app/*.py`), proxy et UI Next.js (`frontend/`), chart Helm et docker-compose.
Versions vérifiées dans un venv : fastapi 0.159.1, starlette 1.10.0, uvicorn 0.54.0 (ASGI spec 2.3), anyio 4.15.1 (pool de threads par défaut : `CapacityLimiter(40)`).

## Ce qui est déjà bien fait

- L'archive est réellement **générée en flux** : `zipfile` écrit dans un puits non positionnable (`_ChunkSink`, `archive.py:160`) et chaque message est envoyé dès qu'il est ajouté (`archive.py:206`). Rien n'est stocké en entier, quelle que soit la taille de la boîte.
- Chaque `.eml` passe par un `SpooledTemporaryFile` (10 Mo en RAM puis disque, `archive.py:226`), lu par blocs de 1 Mo (`archive.py:238`).
- Les handlers sont des `def` synchrones exécutés dans le pool de threads : la boucle d'évènements n'est jamais bloquée par httpx (sync) ni par `time.sleep`.
- Si le client se déconnecte, Starlette (spec ASGI 2.3 avec uvicorn) annule le flux, et le contre-pouvoir d'uvicorn (`flow.drain()`) freine le générateur quand le client lit lentement : pas d'accumulation côté serveur.
- Le proxy Next.js relaie `response.body` en flux, sans le mettre en tampon (`route.ts:55`), et propage l'annulation (`signal: request.signal`, `route.ts:48`). Dans Helm, l'ingress envoie `/api` directement au backend avec `proxy-buffering: off` et 3600 s de délai de lecture (`values.yaml:76-78`).
- Les suggestions sont temporisées (250 ms, `ConditionValueInput.tsx:9`) et ne partent qu'à partir de 2 caractères.

---

## Findings

### [Élevée] Téléchargement des messages strictement séquentiel : débit borné par la latence Zimbra
- Emplacement : `backend/app/archive.py:194-206`, `backend/app/archive.py:226-230`
- Problème : chaque message suit la chaîne « GET ReST → spool → compression → envoi » l'un après l'autre, sur un seul thread. Le débit dépend donc de la latence d'un aller-retour Zimbra (TLS déjà ouvert, mais en-têtes, génération côté Zimbra, premier octet), et non de la bande passante. À 100–200 ms par message, une boîte de 100 000 messages prend **3 à 6 heures**, alors que la bande passante et le CPU restent presque inutilisés. L'UI le dit elle-même (« Les courriels sont récupérés un par un », `DownloadOverlay.tsx:122`). Plus l'export dure, plus le risque d'expiration de jeton, de coupure réseau ou de déplacement de messages augmente (voir le finding sur la pagination).
- Recommandation : précharger un nombre limité de messages en parallèle pendant que le thread principal écrit le ZIP **dans l'ordre**. Avec un `ThreadPoolExecutor` (httpx.Client est thread-safe) et une fenêtre glissante de 4 à 8 messages :
  ```python
  from concurrent.futures import ThreadPoolExecutor
  from collections import deque

  def _fetch(client, token, message_id):
      buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_SIZE)
      try:
          with client.download_eml(token, message_id) as chunks:
              for chunk in chunks:
                  buffer.write(chunk)
      except BaseException:
          buffer.close()
          raise
      buffer.seek(0)
      return buffer

  with ThreadPoolExecutor(max_workers=settings.download_concurrency) as pool:
      pending = deque()
      for message in messages:
          pending.append((message, pool.submit(_fetch, client, token, message.id)))
          if len(pending) >= settings.download_concurrency:
              yield from _write(*pending.popleft())   # .result() puis écriture ZIP
      while pending:
          yield from _write(*pending.popleft())
  ```
  Rendre la concurrence configurable (variable d'environnement + `values.yaml`) pour ne pas surcharger le serveur Zimbra, et la plafonner au nombre de connexions du pool httpx (`httpx.Limits(max_connections=...)`). La RAM maximale par export devient `concurrence × 10 Mo` : à prendre en compte dans les limites Helm (finding plus bas).

### [Élevée] Extraction des pièces jointes : message entier et toutes les PJ décodées en mémoire en même temps
- Emplacement : `backend/app/archive.py:244`, `backend/app/archive.py:248-249`, `backend/app/archive.py:255-267`, `charts/zimbra-archivage/values.yaml:32-33`
- Problème : `BytesParser(...).parse(eml)` construit tout l'arbre MIME en mémoire, avec les charges utiles en `str` (la mise en tampon sur disque du spool ne sert donc plus à rien). Ensuite `list(extract_attachment_files(buffer))` décode **toutes** les pièces jointes avant d'en écrire une seule, puis `archive.writestr(..., content)` en garde encore une copie compressée dans `_ChunkSink` jusqu'au `drain()` suivant. Pour un message de 50 Mo (base64), le pic est d'environ 50 Mo (texte analysé) + 37 Mo (PJ décodées) + la sortie compressée + `b"".join` dans `drain()` (`archive.py:174`), soit **150 Mo et plus pour un seul message**. Avec la limite de 512 Mi du pod et quelques exports simultanés, le risque d'OOMKill est réel, et il tue alors **tous** les exports en cours du pod. `policy.default` est aussi sensiblement plus lent que `compat32`, car il analyse chaque en-tête en objet riche, et ce coût est payé pour chaque message, même sans pièce jointe.
- Recommandation :
  1. Ne lancer l'analyse MIME que si le message a des pièces jointes : `SearchRequest` renvoie le drapeau `f` (contient `a` quand il y a une PJ). Il suffit de l'ajouter à `MessageSummary` et de tester `"a" in message.flags` avant d'appeler `extract_attachment_files`.
  2. Écrire chaque pièce jointe dès qu'elle est produite, en flux, au lieu de les accumuler avec `list(...)` :
     ```python
     for name, content in extract_attachment_files(buffer):
         with archive.open(_zip_info(f"{directory}/{name}", date_time), "w") as entry:
             entry.write(content)
         del content
         yield sink.drain()          # si _add_message devient un générateur
     ```
  3. Plafonner la taille des messages analysés (par exemple 25 Mo, configurable) et noter un avertissement dans `erreurs.txt` au-delà, pour ne pas se faire tuer par l'OOM sur une seule boîte atypique. N'importe quel expéditeur externe peut envoyer un tel message : `security.md` classe ce point en Élevée sous l'angle déni de service et renvoie ici pour les corrections.
  4. Remonter la limite mémoire Helm en fonction (voir le finding Helm).

### [Élevée] Pagination `SearchRequest` par offset, paresseuse et entrelacée avec les téléchargements : archive incomplète ou avec doublons, coût croissant
- Emplacement : `backend/app/zimbra.py:263-284`, `backend/app/zimbra.py:317-326`, `backend/app/archive.py:144`, `backend/app/archive.py:197`
- Problème :
  - Pagination par `offset` sans `sortBy` explicite : côté Zimbra, une page profonde coûte de plus en plus cher (le serveur doit sauter `offset` résultats), et le résultat dépend de l'ordre par défaut (`dateDesc`).
  - La page suivante n'est demandée qu'**après le téléchargement des 500 messages précédents**, soit plusieurs minutes plus tard. Un message arrivé ou déplacé entre-temps décale tous les offsets. On obtient alors un message traité deux fois (entrée ZIP dupliquée : `zipfile` n'émet qu'un `UserWarning: Duplicate name`, et la barre de progression dépasse le total), ou un message silencieusement absent de l'archive. Sur un export de plusieurs heures de la Réception, c'est très probable, et c'est aggravé par la lenteur du téléchargement séquentiel (premier finding). Je le classe en Élevée parce que, pour un outil d'archivage, une archive qui manque des messages sans le signaler est plus grave que la lenteur. `quality.md` renvoie ici et le lit aussi comme un bug de correction.
- Recommandation : utiliser la pagination par curseur de Zimbra (`"cursor": {"id": last_id, "sortVal": last_sort_val}` avec `"sortBy": "dateAsc"`), qui est stable et à coût constant. À défaut, collecter d'abord tous les identifiants d'un dossier (environ 100 octets par `MessageSummary`, donc 10 Mo pour 100 000 messages), puis télécharger. Avec `dateAsc`, les nouveaux messages arrivent en fin de liste et ne décalent plus rien.

### [Moyenne] Recherche complète refaite deux fois : estimation puis export, voire en parallèle pendant l'export
- Emplacement : `frontend/app/dossiers/page.tsx:139-146`, `backend/app/archive.py:150-157`, `backend/app/main.py:243-258`
- Problème : `summarize` parcourt **toutes** les pages de `SearchRequest` (200 requêtes SOAP pour 100 000 messages) uniquement pour compter. Si l'utilisateur n'a pas cliqué sur « Estimer », `handleDownload` relance `getSummary` **en même temps** que l'export : Zimbra exécute alors deux parcours complets en parallèle pour la même session, et le pool de threads du backend reste occupé pendant toute la durée de l'estimation. Même après une estimation, l'export refait tout le parcours.
- Recommandation :
  - Sans filtre ni dates (le cas « toute la boîte » ou « dossier entier »), le total est déjà connu côté client grâce à `message_count` de l'arbre (`page.tsx:28-30`) : l'utiliser au lieu d'appeler `getSummary`.
  - Sinon, faire publier le total par l'export lui-même dans `ProgressRegistry` (par exemple en collectant d'abord les identifiants comme proposé au finding précédent, puis `progress.total = len(ids)`), et le renvoyer dans `/api/export/progress`. Plus besoin de second parcours.
  - En option, mettre en cache le résultat de `summarize` par `(hash(token), paramètres)` pendant quelques minutes.

### [Moyenne] Pool de threads partagé (40 jetons) : sonde de vie bloquée en charge, ce qui redémarre le pod avec les exports en cours
- Emplacement : `backend/app/main.py:114-116`, `backend/app/main.py:243-258`, `backend/app/main.py:306-316`, `charts/zimbra-archivage/templates/backend.yaml:38-45`
- Problème : tous les handlers sont synchrones (`def`), y compris `/api/health`. Starlette exécute aussi chaque `next()` du générateur d'export dans le même pool anyio, limité à 40 threads. Une estimation garde un thread pendant tout le parcours, et un export en garde un pendant chaque message, jusqu'à **363 s** quand Zimbra répond 502/503 (sommeil `3**attempt`, voir plus bas). Sous charge, ou quand Zimbra est lent, ce qui est justement le moment où les retries s'enchaînent, le pool sature. La sonde de vie (délai par défaut de 1 s) échoue alors et Kubernetes tue le pod, ce qui interrompt **tous** les téléchargements. Un seul processus uvicorn est lancé (`backend/Dockerfile:20`) : la compression `zlib` relâche le GIL, mais l'analyse MIME en Python pur ne le fait pas. `security.md` traite l'exploitabilité en Élevée : exports simultanés illimités lancés par un utilisateur authentifié, ou par un site tiers via un GET de premier niveau autorisé par SameSite=Lax. Ce rapport-ci couvre la cause technique et les corrections. `quality.md` signale aussi des sondes de vie et de disponibilité identiques, sans `timeoutSeconds` ni `failureThreshold`.
- Recommandation :
  - Passer `health` en `async def` pour qu'elle réponde depuis la boucle d'évènements, sans dépendre du pool :
    ```python
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}
    ```
  - Ajouter `timeoutSeconds`, `periodSeconds` et `failureThreshold` explicites à la `livenessProbe`, plus tolérants que la `readinessProbe`.
  - Limiter le nombre d'exports et d'estimations simultanés (sémaphore global et par session, 429 au-delà).
  - Si l'on veut plusieurs processus ou réplicas, déplacer d'abord `ProgressRegistry` (en mémoire, `progress.py:1-5`) vers un stockage partagé, sinon le suivi d'avancement passe en « unknown ».

### [Moyenne] Nouveau `httpx.Client` (pool de connexions et contexte TLS) à chaque requête HTTP
- Emplacement : `backend/app/main.py:32-33`, `backend/app/zimbra.py:164-177`, `backend/app/main.py:163-175`
- Problème : `get_client_factory` crée un `ZimbraClient`, donc un `httpx.Client`, par requête. Chaque appel paie la création d'un `SSLContext` (chargement du magasin de certificats, quelques dizaines de ms de CPU), une résolution DNS et une poignée de main TCP+TLS vers Zimbra. Pendant la saisie d'un filtre, `/api/addresses` est appelé environ toutes les 250 ms, et chaque appel fait 2 requêtes SOAP **séquentielles** (`suggestions.py:52-66`) sur une connexion neuve. Le chargement de la page fait `getMe` + `getFolders`, et `handleDownload` refait `getMe` (`page.tsx:134`) : autant de connexions jetables.
- Recommandation : créer un seul `httpx.Client` partagé dans le `lifespan` de l'application (thread-safe, keep-alive), avec des `httpx.Limits` explicites, et l'injecter dans `ZimbraClient` (le paramètre `transport` existe déjà, il suffit d'accepter un client tout fait). Côté front, ajouter un `AbortController` dans `ConditionValueInput.tsx:29-49` pour annuler la requête de suggestions devenue inutile, au lieu de simplement ignorer sa réponse.

### [Moyenne] Délais par défaut d'undici dans le proxy Next.js : estimations longues et pauses de retry coupées à 300 s
- Emplacement : `frontend/app/api/[...path]/route.ts:41-49`, `docker-compose.yml:31-34`
- Problème : le `fetch` de Node (undici) applique par défaut `headersTimeout` = 300 s et `bodyTimeout` = 300 s. `/api/export/summary` ne renvoie ses en-têtes qu'à la fin du parcours complet : sur une grosse boîte, cela dépasse 5 minutes et le proxy répond 502 « Service d'archivage injoignable ». Pendant un export, une série de retries (jusqu'à 363 s sans octet, `zimbra.py:190-192`) dépasse aussi `bodyTimeout` et **tronque le ZIP** sans que l'utilisateur le sache. Le chemin concerné est docker-compose (le frontend est le seul port exposé). Dans Helm, l'ingress contourne le proxy pour `/api`.
- Recommandation : passer un `dispatcher` undici avec des délais adaptés à ce relais :
  ```ts
  import { Agent } from "undici";
  const dispatcher = new Agent({ headersTimeout: 0, bodyTimeout: 0, keepAliveTimeout: 60_000 });
  // fetch(target, { ..., dispatcher } as RequestInit)
  ```
  En parallèle, réduire les pauses de retry côté backend (finding suivant) pour qu'aucune pause ne dépasse quelques dizaines de secondes.

### [Moyenne] Retries à backoff `3**attempt` : jusqu'à 6 minutes de sommeil bloquant par requête, et donc par message
- Emplacement : `backend/app/zimbra.py:188-200`, `backend/app/config.py:25`
- Problème : les pauses sont de 3, 9, 27, 81 puis 243 s (363 s au total), sans gigue ni prise en compte de `Retry-After`. Chaque tentative dispose en plus de `http_timeout=120 s` pour **chaque** phase (connexion, lecture, écriture, pool), donc une connexion impossible attend 120 s avant d'échouer. `range(MAX_TRY + 1)` envoie en réalité **6** requêtes, alors que le message d'erreur dit « malgré 5 essais » (relevé par `quality.md`). Pendant ce temps, un thread du pool est immobilisé (voir le finding sur le pool de threads). Et comme `_send` sert aussi pour chaque `.eml`, un Zimbra durablement indisponible coûte environ 6 minutes **par message** : un export de 1 000 messages restants ne se termine qu'au bout d'environ 100 heures au lieu d'échouer vite. À l'inverse, les erreurs de connexion et les délais dépassés (`httpx.HTTPError`, `zimbra.py:195-196`) ne sont pas retentés du tout. `tests.md` signale qu'aucun test ne fixe ni le nombre d'envois ni la durée des attentes.
- Recommandation : backoff plafonné avec gigue (par exemple `min(2**attempt, 30) * random.uniform(0.5, 1.5)`), respect de `Retry-After`, et délais différenciés :
  ```python
  httpx.Timeout(settings.http_timeout, connect=10.0, pool=10.0)
  ```
  Ajouter aussi un disjoncteur par export : après K messages consécutifs en échec 5xx, abandonner proprement avec un message dans `erreurs.txt` plutôt que de dérouler les retries pour chacun des messages restants.

### [Faible] Recompression DEFLATE de pièces jointes déjà compressées
- Emplacement : `backend/app/archive.py:249`, `backend/app/archive.py:308-311`
- Problème : les pièces jointes extraites (PDF, JPEG, PNG, DOCX/XLSX, ZIP…) sont déjà compressées. Les passer dans DEFLATE (niveau 6 par défaut) consomme du CPU pour un gain proche de zéro, voire négatif. Le `.eml` lui-même (texte, base64) se compresse bien et doit rester en DEFLATE.
- Recommandation : `ZIP_STORED` pour les types déjà compressés (liste des types MIME ou extensions), et éventuellement `compresslevel=1` pour les `.eml` (gain de taille marginal, CPU nettement réduit) :
  ```python
  ALREADY_COMPRESSED = {".zip", ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".docx", ".xlsx", ".pptx", ".mp4", ".gz", ".7z"}
  info.compress_type = zipfile.ZIP_STORED if ext in ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
  ```

### [Faible] Un gros message est entièrement compressé en mémoire avant d'être envoyé
- Emplacement : `backend/app/archive.py:160-176`, `backend/app/archive.py:205-206`, `backend/app/archive.py:237-239`
- Problème : `drain()` n'est appelé qu'après l'écriture complète d'un message (et de ses pièces jointes). Toute la sortie compressée d'un `.eml` de 50 Mo s'accumule donc dans `_ChunkSink._chunks`, plus une copie par `bytes(data)` à chaque `write`, puis une autre par `b"".join` au `drain`. Le spool sur disque est contourné à la sortie. Rien n'est envoyé au client pendant ce temps, ce qui augmente aussi le risque de délai dépassé côté proxy.
- Recommandation : faire de `_add_message` un générateur qui `yield sink.drain()` après chaque bloc de 1 Mo copié dans `entry`, et pour chaque pièce jointe. Dans `_ChunkSink.write`, `bytes(data)` peut être conservé (zipfile peut réutiliser le tampon), mais `drain()` peut renvoyer les morceaux un par un (`yield from`) au lieu de les concaténer.

### [Faible] Une recherche SOAP par dossier, y compris pour les dossiers vides
- Emplacement : `backend/app/archive.py:88-95`, `backend/app/archive.py:141-147`, `backend/app/archive.py:194-197`
- Problème : pour « toute la boîte » ou un dossier avec sous-dossiers, chaque dossier déclenche au moins une `SearchRequest` (`inid:<id>`), même quand `Folder.message_count == 0`. Avec des arborescences de plusieurs centaines de dossiers, ce sont autant d'allers-retours inutiles, multipliés par 2 avec l'estimation.
- Recommandation : sauter les dossiers dont `message_count == 0` (la valeur `n` vient de `GetFolderRequest`, déjà chargée), dans `stream_zip` comme dans `summarize`.

### [Faible] Ressources Helm et `/tmp` non dimensionnés pour la charge réelle
- Emplacement : `charts/zimbra-archivage/values.yaml:28-33`, `charts/zimbra-archivage/templates/backend.yaml:48-54`, `docker-compose.yml:19-20`
- Problème :
  - Limite de 512 Mi pour un backend qui peut garder, par export, jusqu'à 10 Mo de spool + la sortie compressée d'un message + l'arbre MIME complet en cas d'extraction (voir plus haut). Quelques exports simultanés avec extraction suffisent à atteindre l'OOMKill. La requête CPU de 100m, sans limite, est raisonnable, mais la compression d'un export soutenu consomme environ un cœur.
  - `emptyDir` sur `/tmp` sans `sizeLimit` : un spool de gros message peut remplir le disque éphémère du nœud, ce qui provoque une éviction.
  - En docker-compose, `/tmp` est un `tmpfs`, donc **en RAM** : le « débordement sur disque » des `.eml` de plus de 10 Mo prend en réalité de la mémoire.
- Recommandation : fixer `emptyDir.sizeLimit` (par exemple 1Gi) et `resources.requests/limits.ephemeral-storage` ; dimensionner la mémoire selon `concurrence_max × (spool + pic d'analyse)` (par exemple 1Gi avec un sémaphore de 4 exports) ; en compose, donner une taille au tmpfs (`tmpfs: - /tmp:size=512m`) ou utiliser un volume.

### [Info] Avancement : purge seulement au démarrage d'un export, et sondage toutes les secondes
- Emplacement : `backend/app/progress.py:35-51`, `frontend/components/DownloadOverlay.tsx:7`, `frontend/components/DownloadOverlay.tsx:53`
- Problème : les entrées expirées ne sont supprimées que dans `start()`. Sans nouvel export, une entrée terminée reste en mémoire jusqu'au prochain export, ce qui reste négligeable (quelques octets par entrée, au plus une par export de l'heure écoulée). `advance()` modifie l'objet sans verrou (sans conséquence ici grâce au GIL et à l'écrivain unique). Le sondage à 1 s représente un appel léger par export actif, et il s'arrête quand l'overlay est masqué ou terminé (`page.tsx:154-160`).
- Recommandation : rien d'urgent. Pour aller plus loin, passer à un intervalle adaptatif (1 s puis 3 à 5 s après 30 s), ou à un flux SSE `/api/export/progress/stream` qui supprime le sondage.

### [Info] Rendus React : coûts négligeables
- Emplacement : `frontend/app/dossiers/page.tsx:84-93`, `frontend/components/FilterEditor.tsx:80`, `frontend/components/ConditionValueInput.tsx:31-35`
- Problème : chaque frappe dans un filtre fait re-rendre toute la page (`toMessageFilter` est calculé deux fois, une dans `page.tsx` et une dans `FilterEditor`, puis `JSON.stringify`). Avec au plus 10 × 20 conditions, c'est négligeable. Dans `ConditionValueInput`, `setSuggestions([])` crée un nouveau tableau à chaque passage de l'effet, ce qui provoque un rendu supplémentaire alors que la liste est déjà vide. L'arbre des dossiers est replié par défaut et ne monte que les nœuds visibles.
- Recommandation : facultatif. Passer `filter` en prop à `FilterEditor` au lieu de le recalculer, et écrire `setSuggestions((s) => (s.length ? [] : s))`.
