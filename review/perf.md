# Revue Optimisation & Performance

Périmètre : backend FastAPI (`backend/app`), frontend Next.js, chart Helm, Dockerfiles, docker-compose. Revue en lecture seule.

Points positifs : le ZIP est bien produit en flux (`_ChunkSink` + `ZipFile` non positionnable), un seul `httpx.Client` est réutilisé pour tous les téléchargements d'un export, les `.eml` de plus de 10 Mo passent par un fichier temporaire (`SpooledTemporaryFile`), les routes bloquantes sont déclarées en `def` (donc exécutées dans le threadpool), la saisie des suggestions est « debouncée », et le proxy Next relaie le corps en flux.

---

### [Élevée] Le téléchargement d'un export lance en parallèle un second parcours complet de la boîte (estimation)
- Emplacement : `frontend/app/dossiers/page.tsx:139-145` (appel de `getSummary` dans `handleDownload`), `backend/app/archive.py:150-157` (`summarize`)
- Problème : si l'utilisateur clique sur « Télécharger » sans avoir cliqué sur « Estimer », le front lance `getSummary(options)` simultanément à `/api/export`, uniquement pour alimenter la barre de progression. `summarize` refait donc toutes les pages de `SearchRequest`, dossier par dossier, que l'export va refaire juste après. La charge sur Zimbra et sur le backend (un thread bloqué par l'estimation) est doublée, et les deux flux se disputent le quota de la même session Zimbra. Sur une grosse boîte (centaines de milliers de messages), l'estimation peut aussi se terminer après l'archive.
- Recommandation : faire calculer le total par l'export lui-même. Par exemple, `/api/export` pourrait renseigner `total` dans le registre de progression au fur et à mesure du parcours des dossiers (pré-comptage léger via `message_count` des dossiers, voir finding suivant), et `/api/export/progress` renvoyer `{done, total, finished}`. Le front n'a alors plus besoin d'appeler `getSummary` au téléchargement.

### [Élevée] Estimation et export : une recherche par dossier, offset profond, aucun raccourci avec les compteurs déjà connus
- Emplacement : `backend/app/archive.py:97-103` (`query`), `backend/app/archive.py:141-157`, `backend/app/zimbra.py:317-326` (`iter_messages`)
- Problème :
  1. Pour N dossiers, il y a au moins N `SearchRequest` (`inid:<id>` un par un), même pour des dossiers vides ou contenant peu de messages. Une boîte avec 200 dossiers fait 200 allers-retours séquentiels avant même d'estimer.
  2. Sans filtre ni période, `GetFolderRequest` fournit déjà le nombre de messages (`Folder.message_count`, attribut `n`) et sa taille (`s`, non lu aujourd'hui) : l'estimation n'a pas besoin de parcourir quoi que ce soit.
  3. La pagination par `offset` croît en coût côté Zimbra (le serveur doit sauter `offset` résultats à chaque page) : lecture de 500 en 500 sur un dossier de 100 000 messages = 200 requêtes à coût croissant. Elle n'est pas stable si la boîte change pendant l'export (messages sautés ou dupliqués).
  4. `summarize` ne lit que `id`, `folder_id`, `date_ms`, `size`, mais le parcours se fait quand même par pages de `search_batch_size` avec tous les champs par défaut.
- Recommandation :
  - Court-circuiter l'estimation quand il n'y a ni filtre, ni période, ni `only_with_attachments` : somme de `message_count` des dossiers (et de leur taille si `s` est ajouté à `Folder`).
  - Éviter les dossiers vides : `if folder.message_count == 0: continue` avant la recherche (si aucun filtre ne dépend de l'état en cours).
  - Remplacer `inid:` répétés par une seule requête `under:"/Inbox"` (ou `in:` avec sous-dossiers) puis router chaque message vers son dossier via `m["l"]` et une table `{id: Folder}` construite une fois. Cela divise le nombre de requêtes par le nombre de dossiers.
  - Trier explicitement (`"sortBy": "dateAsc"`) pour stabiliser l'offset, voire paginer par curseur sur la date du dernier message.

### [Élevée] Téléchargement strictement séquentiel, un message à la fois, sans aucun parallélisme
- Emplacement : `backend/app/archive.py:197-206` (boucle de `stream_zip`), `backend/app/archive.py:226-239` (`_add_message`)
- Problème : chaque message déclenche un `GET /service/home/~/?id=…` complet, attendu avant le suivant. Le débit est donc borné par `latence Zimbra × nombre de messages` (typiquement 50 à 300 ms par petit mail) : 100 000 mails ≈ 1,5 à 8 h. Le CPU et le réseau sont presque inactifs la plupart du temps.
- Recommandation : télécharger N messages en avance (N = 4 à 8) avec un `ThreadPoolExecutor` (ou `httpx.AsyncClient` + `anyio`) et ne garder que l'écriture dans le ZIP séquentielle (le `ZipFile` n'est pas thread-safe). Borner à quelques connexions pour ne pas surcharger Zimbra. Exemple :
  ```python
  with ThreadPoolExecutor(max_workers=settings.download_concurrency) as pool:
      for message, future in prefetch(pool, messages, window=16):
          buffer = future.result()   # SpooledTemporaryFile déjà rempli
          write_to_zip(archive, buffer, ...)
  ```
  Rendre la concurrence configurable (`DOWNLOAD_CONCURRENCY`, valeur ConfigMap) ; une alternative moins coûteuse serait la route ReST `?fmt=zip` / `fmt=tgz` de Zimbra qui fournit un dossier entier en un seul flux.

### [Moyenne] L'extraction des pièces jointes charge tout le message et toutes les pièces décodées en mémoire
- Emplacement : `backend/app/archive.py:244` (`list(extract_attachment_files(buffer))`), `backend/app/archive.py:253-267`
- Problème : `BytesParser(policy=policy.default).parse(eml)` construit tout l'arbre MIME en mémoire, `get_payload(decode=True)` décode chaque pièce (base64 → ×0,75 en octets, mais copie complète), et `list(...)` conserve toutes les pièces décodées simultanément avant d'écrire la première. Un mail de 25 à 50 Mo (limite classique) consomme facilement 3 à 5 fois sa taille, soit 150 à 250 Mo, à comparer à la limite du pod de 512 Mi (`charts/.../values.yaml:33`), alors que le reste du chemin est conçu pour borner la mémoire à 10 Mo. Plusieurs exports concurrents multiplient ce risque d'OOM-kill, qui interrompt tous les exports du pod.
- Recommandation : remplacer le `list(...)` par une itération qui écrit chaque pièce dans le ZIP dès qu'elle est décodée (le générateur existe déjà, il suffit de consommer `for name, content in extract_attachment_files(buffer): archive.writestr(...)` dans le même `try`) et libère la pièce ensuite. Pour aller plus loin, utiliser `part.get_content()`/`walk()` avec `del` de chaque contenu, et ignorer les pièces au-delà d'une taille configurable en l'indiquant dans `erreurs.txt`.

### [Moyenne] Compression Deflate systématique, y compris pour des contenus déjà compressés, et exécutée dans le threadpool avec le GIL
- Emplacement : `backend/app/archive.py:193` (`ZIP_DEFLATED`), `backend/app/archive.py:308-311` (`_zip_info`)
- Problème : tout est compressé au niveau par défaut (6) : les `.eml` (base64 de PDF, JPEG, Office/ZIP) se compressent mal mais coûtent du CPU, et les pièces extraites (`jpg`, `zip`, `pdf`, `docx`, …) sont recompressées pour rien. Deflate est CPU-bound et tient le GIL dans une partie du temps : avec plusieurs exports en parallèle, la latence des autres routes du backend (sondes, progression, suggestions) augmente. `requests.cpu: 100m` est très bas pour ce profil.
- Recommandation : utiliser `compresslevel=1` à `3` pour les `.eml` (`zipfile.ZipFile(sink, "w", compression=ZIP_DEFLATED, compresslevel=3)` et `info.compress_level = 3`) et `ZIP_STORED` pour les pièces jointes dont le type est déjà compressé (`image/jpeg`, `application/zip`, `application/pdf`, `video/*`, `*.docx`, …). Augmenter la requête CPU du backend (voir finding Helm).

### [Moyenne] Un `yield` (et donc un aller-retour threadpool + trame chunked) par message, parfois de quelques Ko seulement
- Emplacement : `backend/app/archive.py:206` (`yield sink.drain()`), `backend/app/main.py:306-316`
- Problème : chaque message produit un chunk (souvent 2 à 20 Ko compressés). `StreamingResponse` appelle `next()` du générateur synchrone via le threadpool anyio pour chaque chunk ; il y a donc une bascule de thread, un `send` ASGI, une trame HTTP chunked et un passage dans le proxy Next/ingress par message. Pour des centaines de milliers de petits mails, ce surcoût est comparable au travail utile, et gonfle la charge sur le proxy Next (`route.ts`). À l'inverse, un gros message peut produire un seul chunk de plusieurs dizaines de Mo (le `drain()` rassemble tout), ce qui annule le bénéfice du streaming et dope le pic mémoire.
- Recommandation : accumuler dans le sink jusqu'à un seuil (par ex. 256 Ko) avant de `yield`, et vider aussi en cours d'écriture d'une grosse entrée (dans la boucle `entry.write` de `_add_message`, en laissant un callback demander un `drain` partiel). Dans la pratique : `if sink.size >= FLUSH_THRESHOLD: yield sink.drain()`. Exposer `size` sur `_ChunkSink`.

### [Moyenne] Chaque requête API crée un nouveau `httpx.Client` : pas de réutilisation de connexion TLS vers Zimbra
- Emplacement : `backend/app/main.py:32-33` (`get_client_factory`), `backend/app/zimbra.py:173-177`, usages `main.py:126, 152, 158, 173, 250`
- Problème : `login`, `me`, `folders`, `addresses` et `summary` ouvrent chacun un client (et une poignée de main TCP + TLS) puis le ferment. Les suggestions d'adresses, déclenchées à chaque pause de frappe, paient donc 2 handshakes TLS (autocomplete puis recherche, sur le même client ici, donc 1) plus la création d'un contexte SSL (`verify=True` recharge le bundle de certificats, ~10 à 30 ms CPU à chaque `httpx.Client`).
- Recommandation : créer un `httpx.Client` partagé au démarrage (lifespan FastAPI) avec `httpx.Limits(max_connections=…, max_keepalive_connections=…)` et le passer à `ZimbraClient` ; ne plus le fermer dans `__exit__`. Les en-têtes `Cookie` sont déjà fournis requête par requête, donc le partage est sûr tant qu'aucun cookie jar n'est utilisé.

### [Moyenne] Sondes de santé exécutées dans le threadpool partagé avec les exports : risque de redémarrage du pod en pleine charge
- Emplacement : `backend/app/main.py:114-116` (`def health`), `charts/zimbra-archivage/templates/backend.yaml:121-128`
- Problème : `health` est une fonction synchrone, donc exécutée via `anyio.to_thread` dans un pool de 40 threads. Ce pool est partagé avec chaque chunk des exports, les retries bloquants (`time.sleep` jusqu'à 363 s dans `zimbra.py:192`) et toutes les autres routes `def`. Sous charge, la sonde peut attendre un thread libre au-delà du `timeoutSeconds` (1 s par défaut), `livenessProbe` échoue (3 fois par défaut, soit 30 s) et le kubelet tue le pod, interrompant tous les exports en cours. Aucun `timeoutSeconds`, `periodSeconds` ni `startupProbe` n'est défini.
- Recommandation : déclarer `async def health()` (aucun accès bloquant) pour qu'elle soit servie par la boucle d'événements, et ajouter dans le chart `timeoutSeconds: 3`, `failureThreshold: 6` à la liveness, et un `startupProbe`.

### [Moyenne] Aucune limite de concurrence : les exports simultanés saturent threads, mémoire et Zimbra
- Emplacement : `backend/app/main.py:279-332` (`export`), `backend/app/main.py:243-258` (`export_summary`), `backend/app/progress.py:35-43`
- Problème : un utilisateur (ou un script) peut lancer des dizaines d'exports et d'estimations en parallèle. Chacun garde un `httpx.Client`, un spool jusqu'à 10 Mo en RAM, le décodage des pièces, et mobilise le pool de 40 threads de façon intermittente. Il n'y a ni sémaphore global, ni limite par session, ni refus (429). Combiné à la limite mémoire de 512 Mi, un pic provoque un OOM-kill qui coupe tous les exports.
- Recommandation : ajouter un `threading.BoundedSemaphore` (ou une limite par jeton dans `ProgressRegistry`) refusant avec `429 Too Many Requests` au-delà de N exports actifs globalement et 1 à 2 par utilisateur ; idem pour `summary`. Rendre N configurable.

### [Moyenne] Retries : attente bloquante très longue, sans gigue, et pas de retry sur les erreurs de connexion
- Emplacement : `backend/app/zimbra.py:188-200`
- Problème :
  - Le backoff `3**attempt` donne 3, 9, 27, 81, 243 s, soit 363 s d'attente cumulée, dans un thread bloqué par `time.sleep`, sans qu'un octet soit envoyé au client : le navigateur, l'ingress (`proxy-read-timeout: 3600` est suffisant ici) ou un proxy intermédiaire peuvent couper la connexion inactive ; pendant ce temps le thread du pool est inutilisable.
  - Pas de gigue : plusieurs exports qui échouent ensemble réessayent en même temps (effet de troupeau) alors que Zimbra est déjà indisponible.
  - Seuls les statuts 502/503/504 sont retentés ; un `httpx.ConnectError`, `ReadTimeout` ou un reset de connexion (`RemoteProtocolError`) lève immédiatement `ZimbraError`, ce qui abandonne tout le dossier en cours dans `stream_zip` (voir finding suivant), ou le message en erreur dans `_add_message`.
  - Une erreur de connexion en `download_eml` pendant la lecture du flux (`iter_bytes`) n'est pas retentée non plus, alors que le téléchargement est entièrement rejouable.
  - `Retry-After` n'est pas respecté.
- Recommandation : backoff exponentiel plafonné avec gigue (`min(30, 2**attempt) * random.uniform(0.5, 1.5)`), retry sur `httpx.TransportError`/`TimeoutException` (hors erreur TLS), et retry du téléchargement complet d'un message (2 à 3 essais) dans `_add_message` avant de le déclarer en erreur.

### [Moyenne] Une erreur sur une page de recherche abandonne tout le reste du dossier
- Emplacement : `backend/app/archive.py:196-208`
- Problème : le `try/except ZimbraError` entoure la boucle sur les messages du dossier. Si une page de `SearchRequest` échoue (timeout, 5xx persistant), les messages restants du dossier ne sont jamais exportés ; seule une ligne dans `erreurs.txt` le signale. Pour un dossier de 100 000 messages, une indisponibilité de 6 minutes entraîne la perte silencieuse de dizaines de milliers de messages dans l'archive. (Impact fonctionnel, mais lié directement à la stratégie de retry et de pagination.)
- Recommandation : retenter la page échouée (avec l'offset courant) avant d'abandonner, et indiquer dans `erreurs.txt` combien de messages restent non traités (`folder.message_count - traités`).

### [Faible] `DateRange.contains` recalcule les bornes à chaque message
- Emplacement : `backend/app/archive.py:50-76`, appelé par `backend/app/archive.py:146`
- Problème : `_start_ms()`/`_end_ms()` reconstruisent des `datetime` (avec `ZoneInfo`) et appellent `timestamp()` pour chaque message, soit des centaines de milliers de fois pour rien (dataclass figée, valeurs constantes).
- Recommandation : les calculer une fois, via `functools.cached_property` (non compatible avec `frozen=True` + `slots`, utiliser `__post_init__` avec `object.__setattr__`) ou les passer en champs calculés à la construction dans `build_export_request`.

### [Faible] Progression : pas de total côté serveur, sondage fixe à 1 s sans backoff ni pause d'onglet
- Emplacement : `frontend/components/DownloadOverlay.tsx:7, 27-61`, `backend/app/main.py:261-268`
- Problème :
  - Le total vient d'un second appel coûteux (voir premier finding) ; `ProgressResponse` ne l'expose pas.
  - Le sondage `setTimeout` toutes les 1 s continue pendant des heures, y compris onglet masqué, et les erreurs passagères (502, réseau) sont retentées au même rythme indéfiniment (seuls 404 > 15 s et 401 arrêtent). Chaque requête occupe un thread du pool (route `def`) et passe par une nouvelle lecture du registre.
  - `setState` est appelé à chaque réponse même sans changement : `FoldersPage` (donc l'arbre de dossiers et le formulaire complets) est re-rendu chaque seconde.
- Recommandation : intervalle adaptatif (1 s puis 2 à 5 s si `document.hidden`, backoff exponentiel après erreur), n'appeler `setState` que si `done`/`status` changent (`setState(prev => same ? prev : next)`), ou passer en SSE/streaming (`text/event-stream`) pour supprimer le sondage. Passer `export_progress` en `async def` (lecture mémoire, sans I/O).

### [Faible] Re-rendus React : arbre de dossiers et formulaire non mémoïsés
- Emplacement : `frontend/components/FolderTree.tsx:19-72` (`FolderItem`), `frontend/app/dossiers/page.tsx:45-93, 193-197`
- Problème : tout état de `FoldersPage` (frappe d'une date, d'un filtre, tick de progression) re-rend `FolderTree` et tous ses `FolderItem` (non `memo`). Avec plusieurs milliers de dossiers dépliés, cela devient visible. De plus, l'`useEffect` des lignes 90-93 déclenche un rendu supplémentaire à chaque modification d'option (état dérivé qu'il vaudrait mieux calculer ou réinitialiser dans les gestionnaires). `countMessages` est recalculée récursivement à chaque changement de `folders` (déjà dans `useMemo`, correct).
- Recommandation : `export default memo(FolderItem)` avec `onSelect` stable (c'est déjà `setSelected`), déplacer `DownloadOverlay`/`useExportProgress` dans un composant enfant isolé afin que ses ticks ne re-rendent pas la page, et regrouper les options dans un seul état `options` pour remplacer l'effet de réinitialisation.

### [Faible] `Folder.find` / `walk` récursifs et `GetFolderRequest` complet à chaque appel API
- Emplacement : `backend/app/zimbra.py:98-104`, `backend/app/main.py:151-160, 230-236, 251, 293`
- Problème : `me`, `folders`, `summary` et `export` rappellent chacun `get_root_folder` (une requête SOAP + parsing de tout l'arbre). `find` parcourt l'arbre avec des générateurs imbriqués (O(profondeur × n)). Pour une boîte avec des milliers de dossiers, c'est mesurable mais secondaire.
- Recommandation : indexer les dossiers dans un dict `{id: Folder}` lors de `_parse_folder`, et éventuellement mettre l'arbre en cache quelques secondes par jeton (TTL court) pour le triplet `folders` → `summary` → `export`. L'appel `getMe()` dans `handleDownload` (`page.tsx:134`) est un aller-retour de plus que l'`/api/export` ferait lui-même en cas de 401.

### [Faible] Suggestions d'adresses : deux sources interrogées en série, sans annulation côté client
- Emplacement : `backend/app/suggestions.py:114-128`, `frontend/components/ConditionValueInput.tsx:29-49`
- Problème : `autocomplete` puis `search_addresses` sont exécutés l'un après l'autre (la seconde ne l'est que si la première ne remplit pas 10 résultats, ce qui est le cas courant). La latence est la somme des deux. Côté front, un `cancelled` ignore le résultat mais ne coupe pas la requête HTTP (pas d'`AbortController`) : une frappe rapide laisse des recherches `SearchRequest` coûteuses se terminer côté Zimbra.
- Recommandation : lancer les deux sources en parallèle (`ThreadPoolExecutor(2)`), et passer un `signal` d'`AbortController` à `fetch` (via `request`) pour annuler les requêtes obsolètes ; mettre en cache de courte durée (30 à 60 s) les réponses par (jeton, champ, préfixe).

### [Faible] Ressources Helm : requests basses, pas de `terminationGracePeriodSeconds`, pas d'affinité de session ni de plafond sur `/tmp`
- Emplacement : `charts/zimbra-archivage/values.yaml:21-37`, `charts/zimbra-archivage/templates/backend.yaml:121-137`, `charts/zimbra-archivage/templates/frontend.yaml:39-48`
- Problème :
  - `requests.cpu: 100m` et `requests.memory: 128Mi` pour un service qui compresse (CPU) et peut avoir un pic de plusieurs centaines de Mo (QoS Burstable avec un rapport request/limit de 1:4) : risque d'éviction sous pression mémoire du nœud et de throttling en cas de nœud chargé (aucune limite CPU, ce qui est bien, mais la request pilote le partage).
  - Pas de `terminationGracePeriodSeconds` ni de `preStop` : à chaque déploiement/rollout, le pod reçoit SIGTERM et est tué après 30 s, ce qui coupe en plein flux tous les exports (qui durent des minutes à des heures). Uvicorn n'a pas de `--timeout-graceful-shutdown`.
  - `emptyDir` sur `/tmp` sans `sizeLimit` : les spools disque d'exports concurrents peuvent remplir le stockage éphémère du nœud (éviction du pod).
  - `replicaCount: 1` avec progression en mémoire (`progress.py`) : en passant à plusieurs réplicas, un sondage `/api/export/progress` retombe sur un autre pod et renvoie 404 ; le `Service` n'a pas d'affinité (`sessionAffinity: ClientIP`) ni l'ingress d'annotation de cookie d'affinité. Aucun HPA/PDB n'est fourni (un PDB est à éviter avec 1 réplica, mais l'absence de `strategy`/`maxUnavailable` réduit la disponibilité pendant un rollout).
  - Sondes front sans `timeoutSeconds`/`initialDelaySeconds` explicites.
- Recommandation :
  ```yaml
  backend:
    resources:
      requests: { cpu: 250m, memory: 256Mi }
      limits:   { memory: 768Mi }
  # backend.yaml (pod spec)
  terminationGracePeriodSeconds: 3600   # ou drainage explicite avant arrêt
  volumes:
    - name: tmp
      emptyDir: { sizeLimit: 1Gi }
  ```
  Ajouter `sessionAffinity: ClientIP` sur le Service backend (et l'annotation `nginx.ingress.kubernetes.io/affinity: "cookie"` côté ingress) tant que la progression est locale au pod, ou externaliser la progression (Redis) avant d'augmenter les réplicas.

### [Faible] Serveur applicatif : un seul processus uvicorn, pas de limite de requêtes ni de paramétrage du pool
- Emplacement : `backend/Dockerfile:15-16` (`CMD ["uvicorn", …]`), `docker-compose.yml:20-37`
- Problème : un seul worker uvicorn (le GIL partage compression, parsing MIME et JSON entre tous les exports) et le pool de threads anyio est laissé à 40. Dans `docker-compose.yml`, aucune limite de ressources (`mem_limit`, `cpus`) n'est posée : un export peut consommer toute la mémoire de l'hôte. Les réponses JSON ne sont pas compressées (`GZipMiddleware` est inutile pour le ZIP, mais utile pour la liste de dossiers volumineuse via `/api/folders`, d'autant que l'ingress route `/api` vers le backend sans passer par la compression de Next).
- Recommandation : si la concurrence est un objectif, `--workers 2` (en tenant compte du fait que `ProgressRegistry` est en mémoire par processus, donc incompatible tel quel : il faudrait un stockage partagé), ou augmenter le pool via `anyio.to_thread.current_default_thread_limiter().total_tokens` ; ajouter `mem_limit: 768m` au service backend du compose ; ajouter `GZipMiddleware(minimum_size=1024)` limité aux réponses JSON (le ZIP a le type `application/zip`, non concerné si l'on filtre).

### [Info] Le proxy Next.js ajoute un saut pour le flux ZIP en docker-compose, et la taille du ZIP n'est pas annoncée
- Emplacement : `frontend/app/api/[...path]/route.ts:313-344`, `docker-compose.yml:38-48`
- Problème : en déploiement Helm, l'ingress envoie `/api` directement au backend (`charts/.../ingress.yaml:189-196`), donc le proxy n'est pas sur le chemin critique. En revanche, avec docker-compose, tout l'archive transite par Node/undici : correct grâce au streaming (`response.body` relayé), mais ajoute un saut mémoire/CPU et dépend de la contre-pression de `ReadableStream`. L'archive n'ayant pas de `Content-Length`, le navigateur ne peut pas afficher de pourcentage (compensé par l'overlay), et un téléchargement interrompu n'est pas reprenable.
- Recommandation : documenter que le déploiement recommandé route `/api` directement vers le backend (comme l'ingress Helm) ; si le proxy Next doit rester, vérifier avec un export de plusieurs Go qu'aucun tampon ne gonfle dans `route.ts`, et poser `compress: false` dans `next.config.ts` pour éviter toute tentative de compression du flux.

---

## Résumé

1. Les principaux gains sont côté Zimbra : éviter le double parcours (estimation + export), supprimer les recherches par dossier et exploiter les compteurs de `GetFolderRequest`, et paralléliser les téléchargements de `.eml`, aujourd'hui strictement séquentiels.
2. Côté robustesse/mémoire : consommer les pièces jointes au fil de l'eau, réduire le coût de compression, limiter la concurrence des exports, rendre `/api/health` asynchrone et durcir les retries (gigue, erreurs de transport, reprise de page) pour éviter pertes silencieuses et OOM/redémarrages en plein export.
3. Chart et front : relever les requests et ajouter un `terminationGracePeriodSeconds`, un `sizeLimit` sur `/tmp` et une affinité de session ; côté React, isoler le sondage de progression et mémoïser l'arbre de dossiers.
