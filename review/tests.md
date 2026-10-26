# Revue : couverture et qualité des tests

Périmètre : `backend/tests/` (49 tests pytest, tous verts, ~2,3 s), frontend, CI.
Mesure : `pytest --cov=app --cov-branch` (pytest-cov installé dans un venv hors dépôt, absent de `requirements-dev.txt`). Artefacts de couverture supprimés du dépôt après usage.

## Couverture par module (lignes + branches)

| Module | Instr. | Manquées | Branches (partielles) | Couverture | Lignes non couvertes |
|---|---|---|---|---|---|
| `app/archive.py` | 212 | 12 | 70 (3) | 94 % | 207-208, 233-234, 245-246, 288-292, 299, 317 |
| `app/config.py` | 28 | 1 | 0 | 96 % | 49 (`get_settings`) |
| `app/filters.py` | 30 | 0 | 2 | 100 % | |
| `app/main.py` | 154 | 3 | 14 | 98 % | 33 (factory réelle), 63 (handler `ZimbraError` -> 502), 116 (`/api/health`) |
| `app/progress.py` | 40 | 1 | 4 | 98 % | 62 (`get_progress_registry`) |
| `app/suggestions.py` | 32 | 0 | 8 | 100 % | |
| `app/zimbra.py` | 175 | 7 | 30 (2) | 96 % | 146-147, 195-196, 220-221, 235, 313->310 |
| **Total backend** | **671** | **24** | **128 (5)** | **96 %** | |
| Frontend (`frontend/`, ~1260 lignes TS/TSX) | | | | **0 %** (aucun test) | |

Le chiffre de 96 % est flatteur : plusieurs chemins critiques sont « couverts » seulement par le chemin heureux, sans assertion sur les cas limites (voir findings).

---

### [Élevée] Aucune CI sur push / pull request : les tests ne tournent qu'à la publication d'un tag
- Emplacement : `.github/workflows/release.yml:3-12` (seul workflow, déclencheurs `push: tags` et `workflow_dispatch`)
- Problème : pytest et `npm run lint` ne s'exécutent que dans le job `test` de la release. Une régression n'est détectée qu'au moment de publier. Aucun contrôle sur les PR, aucun seuil de couverture, pas de linter/typage Python (ruff, mypy).
- Recommandation : ajouter `.github/workflows/ci.yml` sur `push`/`pull_request` (pytest avec `--cov --cov-fail-under=90`, `tsc`, `next build`, éventuellement `helm lint charts/zimbra-archivage`). Ajouter `pytest-cov` (et ruff/mypy) à `requirements-dev.txt`.

### [Élevée] Frontend entièrement sans test ; le « lint » n'est qu'un `tsc --noEmit`
- Emplacement : `frontend/package.json:9` (`"lint": "tsc --noEmit"`, aucun script `test`, aucun framework de test en devDependencies) ; `frontend/lib/api.ts`, `frontend/components/FilterEditor.tsx`, `frontend/app/dossiers/page.tsx`, `frontend/app/api/[...path]/route.ts`
- Problème : 0 % de couverture sur ~1260 lignes. Logique non triviale jamais testée : construction de la query string d'export (`exportQuery`, `api.ts:101`), sérialisation du filtre vers le JSON attendu par le backend (contrat avec `MessageFilter`), gestion du 401 / session expirée, polling de progression, proxy de streaming (`route.ts` : filtrage des en-têtes, propagation de l'annulation, 502 si backend injoignable). Pas d'ESLint non plus.
- Recommandation : Vitest + Testing Library pour `lib/api.ts` (fonctions pures : `exportQuery`, `formatSize`, `newExportId`) et `FilterEditor`, test du handler `route.ts` avec `fetch` mocké, ajouter ESLint (`next lint`/eslint-config-next). Un test de contrat (JSON de filtre produit par le front validé par `MessageFilter`) éviterait les dérives.

### [Élevée] Session Zimbra expirée en cours d'export : comportement non testé, et probablement silencieux
- Emplacement : `backend/app/archive.py:207-208` (non couvert) ; `backend/tests/test_api.py:90` (seul test d'expiration, sur `/api/folders`)
- Problème : `SessionExpired` hérite de `ZimbraError` ; pendant `stream_zip` elle est capturée par `except ZimbraError` au niveau dossier et transformée en ligne de `erreurs.txt`, sans que la réponse HTTP (déjà en 200) puisse signaler l'échec. L'utilisateur obtient une archive partielle « réussie ». Aucune des branches `except ZimbraError` au niveau dossier (lignes 207-208) n'est exécutée par les tests, alors que c'est le chemin « erreur de recherche en cours d'export ».
- Recommandation : tests d'intégration où `FakeZimbra` renvoie `AUTH_EXPIRED` / `search_fault` pendant le parcours d'un 2e dossier, et assertion sur le contenu de `erreurs.txt` et l'état de progression (`finished`). Décider ensuite du comportement voulu (abandon explicite du flux plutôt que ZIP valide mais tronqué) et le figer par un test.

### [Moyenne] Erreurs réseau et extraction de pièces jointes : branches d'erreur non couvertes
- Emplacement : `backend/app/archive.py:233-234` (`httpx.HTTPError` pendant le téléchargement), `archive.py:245-246` (échec d'extraction des PJ), `backend/app/zimbra.py:195-196` (exception de connexion dans `_send`), `zimbra.py:220-221` (réponse non JSON), `zimbra.py:235` (statut/réponse inattendus)
- Problème : seules les erreurs HTTP 500 sur le REST sont simulées (`failing_message_ids`). Coupure de connexion en milieu de flux (`ReadError`), timeout, corps non JSON, `.eml` illisible/corrompu pour l'extraction ne sont jamais testés. C'est précisément le « garde-fou » de la conception (« jamais d'entrée tronquée dans l'archive »), non vérifié.
- Recommandation : étendre `FakeZimbra` (ou utiliser `httpx.MockTransport` direct) avec un flux qui lève `httpx.ReadError` après quelques octets ; vérifier que l'entrée est absente du ZIP, que `erreurs.txt` la mentionne et que la progression avance quand même. Patcher `extract_attachment_files` pour lever et vérifier que le `.eml` reste présent avec la mention « pièces jointes non extraites ».

### [Moyenne] Retries : seul le cas nominal est testé, rien sur le flux REST ni sur le plafond des délais
- Emplacement : `backend/tests/test_zimbra.py:11-34` ; `backend/app/zimbra.py:188-201`
- Problème : le test vérifie `sleeps == [3, 9]` (SOAP login) et un abandon après 504. Non vérifiés : la séquence complète (3, 9, 27, 81, 243 s, soit ~6 min d'attente cumulée, sans test qui documente ce choix), le retry sur `download_eml` en `stream=True` (fermeture de la réponse précédente, `response.close()`), l'absence de retry sur les autres codes (500, 401), le fait que le corps de requête est bien rejoué. `test_gives_up_after_max_tries` ne vérifie ni le nombre de tentatives (MAX_TRY + 1 = 6 envois) ni les sleeps.
- Recommandation : compter les appels du handler et la liste des `sleeps` (`[3, 9, 27, 81, 243]`, 6 appels) ; ajouter un cas 503 puis 200 sur `download_eml` ; un cas 500 sans retry.

### [Moyenne] Filtrage par dates : le fake ignore `after:`/`before:`, la logique repose sur un seul cas
- Emplacement : `backend/tests/fake_zimbra.py:184-185` (`if name in ("after", "before"): return True`) ; `backend/tests/test_api.py:206-212, 244-253` ; `backend/app/archive.py:50-76, 146`
- Problème : le fake renvoie tous les messages quel que soit le critère de date ; la vraie filtration est donc le re-filtrage `DateRange.contains`. Les tests ne couvrent qu'un jeu de dates (bornes incluses via le message du 15/03 à 23:30). Non testés : `start` seul, changements d'heure (DST : 31 mars 2024, 27 oct 2024), `start == end`, message à 00:00 exact, fuseau configuré différent (`zone_id`), `date_ms` à 0 / négatif. Il n'y a aucun test unitaire direct de `DateRange`. Les termes `after:10/01/24` sont comparés en chaîne, jamais à un vrai serveur (format `dd/MM/yy` dépend de la locale `fr_FR`).
- Recommandation : tests unitaires paramétrés de `DateRange.contains` / `query_terms` (bornes, DST, un seul côté borné) et faire évaluer `after/before` au fake d'après `date_ms`, pour que l'API soit testée de bout en bout sans dépendre du re-filtrage.

### [Moyenne] Filtres : valeurs d'échappement, limites de taille et injection de requête non testées
- Emplacement : `backend/tests/test_filters.py:11-17, 36-42` ; `backend/app/filters.py:85-92, 107, 115` ; `backend/app/main.py:195-215`
- Problème : `quote_value` n'est pas testé pour la barre oblique inverse, les parenthèses, les valeurs Unicode (`\w`), les valeurs à guillemet final, ni les caractères de contrôle. Les bornes (`max_length=200`, `20` conditions, `10` groupes, `filter` > 20000 caractères -> 422) ne sont pas vérifiées. `test_invalid_filter` ne couvre que 2 cas. Aucun test ne vérifie qu'une valeur hostile ne peut pas sortir de son terme (ex. `x) or (from:*`), alors que la valeur est injectée dans la requête de recherche Zimbra.
- Recommandation : cas paramétrés pour `quote_value` (`a\\b`, `a)b`, `é`, `"`), tests aux bornes des contraintes pydantic, et test de propriété simple : la requête produite par une valeur arbitraire reste un seul terme (parsable par `QueryParser` du fake avec exactement une condition).

### [Moyenne] Pièces jointes : couverture limitée à un seul .eml synthétique
- Emplacement : `backend/tests/test_api.py:300-335, 419-446` ; `backend/app/archive.py:253-317` (lignes 288-292, 299, 317 non couvertes)
- Problème : un unique `ATTACHMENT_EML` sert à tous les tests. Non testés : troncature des noms > 100 caractères (288-292), collision de noms sans extension (299), date antérieure à 1980 (317 ; pas de test avec `date_ms` très ancien), pièces jointes imbriquées (multipart dans multipart, `message/rfc822` imbriqué), images inline sans `Content-Disposition`, noms encodés RFC 2047/2231 avec caractères exotiques, noms réservés Windows (`CON`, `NUL`, `aux.txt`), noms de dossier `..`, `.eml` sans corps, pièce jointe très volumineuse (> `SPOOL_MAX_SIZE` = 10 Mo : le passage `SpooledTemporaryFile` -> disque n'est jamais exercé). Les assertions testent surtout les noms ; seul le contenu de `devis.pdf` est comparé.
- Recommandation : tests unitaires directs de `attachment_file_name`, `_unique_name`, `_zip_date_time`, `extract_attachment_files` (paramétrés), un test avec `monkeypatch.setattr(archive, "SPOOL_MAX_SIZE", 10)` pour forcer l'écriture sur disque, et un test « zip-slip » complet vérifiant qu'aucun nom de l'archive ne contient `..` ni ne commence par `/`.

### [Moyenne] Handler d'erreur Zimbra (502), cookie de session et déconnexion : assertions incomplètes
- Emplacement : `backend/app/main.py:61-65` (non couvert), `main.py:133-141`, `backend/tests/test_api.py:68-107`
- Problème : `zimbra_error_handler` (réponse 502 « Erreur Zimbra : … ») n'est jamais exécuté : aucun test ne vérifie qu'une panne Zimbra donne un 502 JSON propre sur login, `/folders` ou `/export/summary`. Le test du cookie ne vérifie pas `SameSite=lax`, `Max-Age` (dérivé de `lifetime_ms`), ni `Secure` selon `cookie_secure` (les fixtures forcent `cookie_secure=False`, donc la valeur de prod `True` par défaut n'est jamais testée). `test_logout_clears_cookie` ne vérifie pas l'en-tête `Set-Cookie` de suppression, il déduit juste du 401 suivant (le cookie jar de TestClient fait l'essentiel). Pas de test pour un login sans champ / champ vide (422), ni pour l'absence de fuite du mot de passe dans une erreur.
- Recommandation : ajouter un test 502 (fault `service.FAILURE`), un test `Settings(cookie_secure=True)` vérifiant `Secure; SameSite=lax; Max-Age=172800`, et vérifier l'en-tête de suppression du cookie au logout.

### [Moyenne] Progression : annulation / interruption du téléchargement et concurrence non testées
- Emplacement : `backend/app/main.py:300-320` ; `backend/tests/test_api.py:256-285` ; `backend/tests/test_progress.py`
- Problème : le `finally` qui marque l'export comme terminé « en cas de téléchargement interrompu » (`main.py:317-320`) et la fermeture du client HTTP ne sont jamais testés avec une vraie interruption (le TestClient lit toujours tout le flux). Rien sur : deux exports simultanés avec le même `export_id` (le second écrase le premier silencieusement), progression observée en cours d'export (le test ne lit la progression qu'après la fin, donc `done` intermédiaire et `finished=False` jamais vus), nettoyage (`start` seulement déclenche l'expiration), thread-safety de `advance` (hors verrou).
- Recommandation : tester `content()` en consommant le générateur partiellement puis `close()` (vérifier `finished == True` et `client.close()` appelé) ; lire la progression depuis `on_message` pour valider les valeurs intermédiaires ; test de collision d'`export_id`.

### [Moyenne] `FakeZimbra` : mock volumineux et trop permissif, risque de faux sentiment de sécurité
- Emplacement : `backend/tests/fake_zimbra.py:46-214`
- Problème : le fake (214 lignes dont un parseur de requêtes Zimbra maison) est correct pour l'essentiel mais ne reproduit pas : `after:/before:` (cf. ci-dessus), les erreurs SOAP renvoyées avec un HTTP 200, la pagination avec `more` réel de Zimbra, les `Set-Cookie`/en-têtes, les partages `link`/`search` dans les résultats, les messages sans `su`/`d`. `QueryParser` est plus tolérant ou plus strict que Zimbra sur la syntaxe (ex. `not` en préfixe, jokers, accents), donc les tests de filtre valident la compatibilité avec le fake, pas avec Zimbra. Les assertions sur la chaîne de requête exacte (`test_api.py:142, 157, 212, 360, 416`) figent l'implémentation. Aucun test de contrat sur de vraies réponses Zimbra (fixtures enregistrées).
- Recommandation : ajouter quelques fixtures JSON réelles (anonymisées) de `SearchResponse`/`GetFolderResponse`/`Fault` comme tests de contrat du parseur ; documenter les écarts du fake ; réduire les assertions sur la chaîne de requête aux seuls tests dédiés à `ExportRequest.query` (tests unitaires sans HTTP).

### [Faible] État global partagé et dépendances de tests fragiles
- Emplacement : `backend/tests/test_api.py:48-58` (`app.dependency_overrides` global, nettoyé par `.clear()`), `backend/app/progress.py:58` (registre singleton), `backend/tests/test_zimbra.py:8` (`SETTINGS` module-level, lit un éventuel `.env` via `env_file=".env"`)
- Problème : `dependency_overrides.clear()` efface aussi d'éventuels overrides posés ailleurs ; les tests ne peuvent pas tourner en parallèle (xdist) avec ce modèle. `Settings(...)` lit `.env` du répertoire courant : un `.env` local peut influencer les tests (ex. `COOKIE_SECURE`, `SEARCH_BATCH_SIZE`). `test_export_progress` dépend de l'ordre d'itération des messages (4 messages, `done == 4`). Le test des dates repose sur des heures proches de minuit en `Europe/Paris` : correct, mais dépendant de la base tzdata.
- Recommandation : `Settings(_env_file=None, ...)` dans les tests, supprimer seulement les overrides posés (`app.dependency_overrides.pop`), ou une fixture `autouse` d'isolation.

### [Faible] Chemins « glue » non couverts : `/api/health`, factory réelle, `get_settings`
- Emplacement : `backend/app/main.py:33, 116` ; `backend/app/config.py:49` ; `backend/app/progress.py:62`
- Problème : le healthcheck, utilisé par Docker/Helm (probes), n'a aucun test ; la fabrique de client réelle et la config lue depuis l'environnement non plus (validateurs de `Settings` : `strip_trailing_slash`, `clamp_file_name_length` 50..180 sont testés indirectement seulement). `search_batch_size` hors bornes (0, 1001) non testé.
- Recommandation : test `GET /api/health`, test de `Settings` avec variables d'environnement via `monkeypatch.setenv` (bornes, slash final, `zone_id` invalide).

### [Faible] Cas ZIP/dossiers limites non testés
- Emplacement : `backend/app/archive.py:88-95, 193-211` ; `backend/tests/test_api.py:228-254`
- Problème : pas de test pour un dossier vide (ZIP valide sans entrée), dossier racine avec `include_subfolders=false` (`folders()` renvoie la racine, sans messages), message dont `folder_id` diffère du dossier demandé (re-filtrage `archive.py:146`), dossiers dont la sanitisation produit des collisions (`a:b` et `a?b` -> `a_b`), `max_file_name_length` extrême, sujet vide ou très long, `erreurs.txt` combinant erreur de dossier et erreur de message, ordre déterministe des entrées. `ExportRequest.folders` n'a pas de test unitaire direct.
- Recommandation : tests unitaires sur `ExportRequest.folders/query`, `eml_file_name`, `folder_archive_path` et sur la validité du ZIP (`ZipFile.testzip()`) dans les cas vides.

### [Faible] Avertissement de dépréciation à la collecte (`httpx` avec `starlette.testclient`)
- Emplacement : sortie de pytest ; `backend/requirements.txt` (`httpx==0.28.1`), `fastapi/testclient.py:1`
- Problème : `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`. Les tests risquent de casser à la prochaine montée de version de FastAPI/Starlette. `pytest.ini` ne configure ni `filterwarnings = error`, ni marqueurs.
- Recommandation : planifier la migration `httpx2` pour le TestClient (dépendance de dev) et ajouter `filterwarnings` pour attraper les nouvelles dépréciations en CI.

### [Info] Points positifs
- Suite rapide (2,3 s) et déterministe, injection de dépendances propre (`sleep`, `clock`, `transport`) qui évite tout `sleep` réel ; pas de mock de bas niveau (`patch`) : le fake agit au niveau transport HTTP, ce qui exerce le vrai code client.
- Bons tests d'isolation de sessions pour la progression (`test_export_progress_of_another_session`), d'exclusion de la corbeille/spam, de path traversal basique sur un nom de pièce jointe (`../devis.pdf`), de dédoublonnage de noms, de ZIP en erreur partielle (`erreurs.txt`).
- Branches : 128 branches, seulement 5 partielles.

## Synthèse des priorités
1. Mettre en place une CI sur PR avec seuil de couverture (+ `pytest-cov` dans les dépendances de dev).
2. Tests frontend (au minimum `lib/api.ts`, `FilterEditor`, proxy `route.ts`) et vrai lint.
3. Tester les chemins d'erreur pendant le streaming (session expirée, coupure réseau, extraction PJ en échec, interruption client) : ce sont les plus risqués et les moins couverts.
4. Tests unitaires directs de `DateRange`, `quote_value`, `attachment_file_name` et des bornes de configuration, pour ne plus dépendre du seul test d'intégration via le fake.
