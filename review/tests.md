# Revue — Couverture & qualité des tests

Périmètre : `backend/tests/*` (pytest + `fake_zimbra.py`), absence de tests côté `frontend/`, CI (`.github/workflows/release.yml`).
Revue read-only : aucun fichier source ni test modifié. Les vérifications empiriques ont été faites par un script jetable hors dépôt (scratchpad).

## Couverture mesurée

Commande (venv hors dépôt, Python 3.14.8 local — la CI et l'image Docker utilisent 3.13) :
`pytest -q --cov=app --cov-branch --cov-report=term-missing -p no:cacheprovider` depuis `backend/`.
Résultat : **49 tests passés, 0 échec, 1 avertissement** (`StarletteDeprecationWarning` : `httpx` avec `starlette.testclient` est déprécié), 0,74 s.

| Module | Instr. | Manquées | Branches | Br. partielles | Couverture | Lignes non couvertes |
|---|---:|---:|---:|---:|---:|---|
| `app/archive.py` | 203 | 12 | 70 | 3 | 94 % | 207-208 (erreur Zimbra sur un dossier), 233-234 (erreur réseau en cours de téléchargement), 245-246 (échec d'analyse MIME), 288-292 (troncature des noms de PJ), 299 (PJ sans extension), 317 (date < 1980) |
| `app/config.py` | 26 | 1 | 0 | 0 | 96 % | 49 (`get_settings`, toujours surchargé) |
| `app/filters.py` | 28 | 0 | 2 | 0 | 100 % | — |
| `app/main.py` | 134 | 3 | 14 | 0 | 98 % | 33 (fabrique de client réelle), **63 (gestionnaire `ZimbraError` → 502)**, 116 (`/api/health`) |
| `app/progress.py` | 40 | 1 | 4 | 0 | 98 % | 62 (registre global) |
| `app/suggestions.py` | 32 | 0 | 8 | 0 | 100 % | — |
| `app/zimbra.py` | 162 | 7 | 30 | 2 | 95 % | 146-147 (couleur invalide), 195-196 (erreur de connexion), 220-221 (réponse non JSON), 235 (réponse sans `…Response`), 313→310 (groupe sans adresse) |
| **Total** | **625** | **24** | **128** | **5** | **96 %** | |
| `frontend/` (TS) | — | — | — | — | **0 % (aucun test)** | proxy `app/api/[...path]/route.ts`, `lib/api.ts`, composants |

Lecture : le taux est élevé, mais les lignes non couvertes sont précisément les **chemins d'erreur** (réseau, Zimbra indisponible, session expirée en cours d'export, MIME invalide). Plusieurs comportements couverts ne sont pas réellement *vérifiés* (fuseau horaire, troncature des noms, sémantique de recherche Zimbra simulée trop généreusement — voir ci-dessous).

---

## Findings

### [Élevée] Les tests ne tournent jamais sur les push / pull requests
- Emplacement : `.github/workflows/release.yml:5-12`, `.github/workflows/release.yml:22-45`
- Problème : le seul workflow se déclenche sur `push` de tags `v*` et `workflow_dispatch`. Le job `test` n'est donc exécuté qu'au moment de publier une release : aucune branche ni PR n'est validée, une régression n'est découverte qu'au tag. Il n'y a ni rapport ni seuil de couverture, et `pytest-cov` n'est pas dans `requirements-dev.txt`.
- Recommandation : ajouter un workflow `ci.yml` sur `push` + `pull_request` qui lance `pytest --cov=app --cov-branch --cov-fail-under=95` (ajouter `pytest-cov` aux dépendances de dev) et `npm ci && npm run lint && npm run build` côté frontend ; faire dépendre `release.yml` de ce même job réutilisable (`workflow_call`).

### [Élevée] Session expirée en cours d'export : ZIP « réussi » ne contenant que `erreurs.txt`, comportement non testé
- Emplacement : `backend/app/archive.py:196-208`, `backend/app/zimbra.py:69-70`
- Problème : `SessionExpired` hérite de `ZimbraError` ; dans `stream_zip`, l'exception levée par la recherche d'un dossier est avalée (`except ZimbraError`) et chaque dossier est noté en erreur. Vérifié avec le faux serveur et un jeton expiré : la réponse est un ZIP valide contenant uniquement `erreurs.txt` (`Dossier /Inbox : no valid authtoken present`, idem pour chaque sous-dossier). Côté ReST, un 401 produit une erreur par message. L'utilisateur reçoit un HTTP 200 et une archive vide ou partielle, sans redirection vers la connexion. Aucun test ne couvre ce cas (lignes 207-208 non couvertes) : `test_expired_session_returns_401` ne teste que `/api/folders`.
- Recommandation : décider du comportement (interrompre le flux sur `SessionExpired` en le relançant hors du `except ZimbraError`, ou au moins l'écrire en tête de `erreurs.txt`) et le figer par un test, par exemple : invalider le jeton dans `FakeZimbra` après le premier `SearchRequest` (`fz.valid_tokens.clear()`), lancer `/api/export` puis vérifier soit l'absence de `erreurs.txt` « silencieux », soit le message attendu.

### [Élevée] Le faux Zimbra simule « contient » par une recherche de sous-chaîne, ce que Zimbra ne fait pas
- Emplacement : `backend/tests/fake_zimbra.py:176-189`, `backend/app/filters.py:1-6` et `:35-38`
- Problème : `QueryParser._term` retourne `value in fields[name].lower()` (sous-chaîne, insensible à la casse et aux accents près) et ignore le joker. La recherche Zimbra (Lucene) est par **mots/tokens** : `subject:budg` ne trouve pas « Budget », `from:lice` ne trouve pas `alice@…`. Les tests de filtres (`test_summary_with_and_filter`, `test_filter_combined_with_and_between_groups`, `test_export_with_or_filter`) passent donc avec une sémantique que la production n'a pas, alors que l'interface parle de « contient ». Un bug utilisateur (« le filtre ne trouve rien ») est ainsi masqué.
- Recommandation : rendre le faux plus fidèle (tokeniser le champ sur `[^\w]+` et comparer mot à mot, préfixe seulement si la valeur finit par `*`) et ajouter un test qui documente explicitement le comportement attendu, ex. `{"field":"subject","value":"budg"}` → 0 message, `budg*` → 1. Si le produit veut vraiment « contient », ajouter le `*` automatiquement côté `Condition.query()` et le tester.

### [Moyenne] Le fuseau horaire n'est pas réellement testé, et les tests dépendent de l'environnement
- Emplacement : `backend/tests/test_api.py:19-20` et `:38-45`, `backend/app/config.py:11` et `:19`, `backend/app/archive.py:50-59`, `:131-138`
- Problème : (1) `Settings` lit les variables d'environnement et un `.env` **relatif au répertoire courant** ; les fixtures ne fixent que quelques champs. Vérifié : `ZONE_ID=Asia/Tokyo pytest` → **5 échecs** (`test_summary_with_date_range`, `test_export_zip_with_subfolders`, …) ; un `.env` de développeur avec `ZONE_ID`, `MAX_FILE_NAME_LENGTH` ou `VERIFY_TLS` change le résultat des tests. (2) À l'inverse, `ZONE_ID=UTC` et `ZONE_ID=America/New_York` → **49 passés** : aucun message de test n'est placé de sorte que Paris et UTC donnent un jour différent, donc la conversion de fuseau (bornes de période et préfixe `yyyyMMdd` des `.eml`) n'est pas vérifiée. (3) Aucun test sur un jour de changement d'heure (le 31/03/2024 dure 23 h, vérifié via `DateRange`) ni sur les bornes exactes (00:00:00.000 inclus, 23:59:59.999 inclus, minuit du lendemain exclu).
- Recommandation : dans les fixtures, passer `_env_file=None` et `zone_id="Europe/Paris"` explicitement (ou `monkeypatch.delenv` des variables `ZONE_ID`, `MAX_FILE_NAME_LENGTH`, etc.) ; ajouter des tests unitaires `DateRange` paramétrés :
  ```python
  @pytest.mark.parametrize("moment, inclus", [
      (datetime(2024, 3, 15, 0, 0, tzinfo=PARIS), True),       # 14/03 23:00 UTC
      (datetime(2024, 3, 15, 23, 59, 59, 999000, tzinfo=PARIS), True),
      (datetime(2024, 3, 16, 0, 0, tzinfo=PARIS), False),
  ])
  def test_date_range_bounds(moment, inclus):
      r = DateRange(date(2024, 3, 15), date(2024, 3, 15), PARIS)
      assert r.contains(int(moment.timestamp() * 1000)) is inclus
  ```
  plus un message à 00:30 heure de Paris dont le nom doit commencer par le jour de Paris, et un cas DST (31/03 et 27/10).

### [Moyenne] Le faux Zimbra ignore `after:` / `before:` : le format de date envoyé n'est jamais validé
- Emplacement : `backend/tests/fake_zimbra.py:184-185`, `backend/app/archive.py:61-68`, `backend/app/zimbra.py:271`
- Problème : le faux renvoie `True` pour tout terme de date ; seul `test_summary_with_date_range` vérifie la chaîne `after:10/01/24 and before:16/03/24`. Si Zimbra interprétait le format autrement (dépend de la `locale` `fr_FR` envoyée), la recherche pourrait renvoyer moins de messages que la période et le refiltrage local ne peut pas les récupérer : perte silencieuse de courriels, invisible en test.
- Recommandation : faire évaluer `after:`/`before:` par le faux (parse `%d/%m/%y`, comparaison stricte en heure du serveur), et ajouter un test où un message est le premier/dernier jour de la période, pour qu'un décalage d'un jour dans `query_terms` fasse échouer le test. Idéalement, un test d'intégration optionnel (marqueur `@pytest.mark.zimbra`) contre un vrai Zimbra de recette.

### [Moyenne] Erreur réseau en cours de téléchargement et atomicité des entrées non testées
- Emplacement : `backend/app/archive.py:224-234`, `backend/app/zimbra.py:193-196`
- Problème : la garantie « ne jamais laisser d'entrée tronquée » (commentaire l. 224-225) n'a aucun test ; les lignes 233-234 et 195-196 ne sont pas couvertes. Vérifié manuellement : avec un flux ReST qui lève `httpx.ReadError` après quelques octets, l'entrée est bien absente et `erreurs.txt` contient `Erreur réseau pendant le téléchargement : coupure`. Le comportement est correct aujourd'hui, mais rien n'empêche une régression (ex. écrire directement dans `archive.open()` pour gagner en mémoire). Par ailleurs, les erreurs de connexion/timeout ne sont pas retentées (seuls 502/503/504 le sont), ce qui n'est pas documenté par un test.
- Recommandation : ajouter un test avec `httpx.Response(200, stream=Boom())` où `Boom.__iter__` émet un chunk puis lève `httpx.ReadError` ; vérifier que `namelist()` ne contient pas l'entrée, que `testzip()` est `None` et que `erreurs.txt` mentionne le fichier. Ajouter un test `ConnectError` → `ZimbraError` (et décider s'il doit être retenté).

### [Moyenne] Gestionnaire `ZimbraError` → HTTP 502 jamais exercé
- Emplacement : `backend/app/main.py:61-65` (ligne 63 non couverte), `backend/app/zimbra.py:218-235`
- Problème : aucun test ne vérifie la réponse d'une erreur Zimbra non liée à la session (Zimbra indisponible après retries, réponse HTML non JSON, `Fault` générique) au niveau de l'API. `test_gives_up_after_max_tries` teste seulement le client. Le contrat HTTP utilisé par le frontend (`502` + `detail` « Erreur Zimbra : … ») n'est donc pas garanti ; les branches 220-221 (non JSON) et 235 (réponse inattendue) ne sont pas couvertes.
- Recommandation : tests API paramétrés : `GetFolderRequest` renvoyant 504 en boucle, `500` HTML, `Fault` `mail.NO_SUCH_FOLDER` → `502` et `detail` commençant par `Erreur Zimbra :`. Ajouter aussi `service.AUTH_EXPIRED` (seul `AUTH_REQUIRED` est testé) et vérifier que la réponse 401 **supprime le cookie** (`Set-Cookie: zimbra_session=""; Max-Age=0; Path=/api`), ce qu'aucun test n'asserte.

### [Moyenne] Le nombre d'essais n'est pas vérifié — décalage de un entre code et message
- Emplacement : `backend/app/zimbra.py:22`, `:188-200`, `backend/tests/test_zimbra.py:26-35`
- Problème : `range(MAX_TRY + 1)` effectue **6 envois** (vérifié) alors que le message dit « malgré 5 essais ». `test_gives_up_after_max_tries` ne compte ni les appels ni les attentes (3+9+27+81+243 = 363 s cumulés en production), il ne peut donc pas détecter ce décalage ni une modification du backoff. Les retries du téléchargement ReST (`stream=True`, `response.close()`) ne sont pas testés.
- Recommandation : compter les requêtes du `MockTransport` et les `sleep` dans le test (`assert len(calls) == MAX_TRY` et `sleeps == [3, 9, 27, 81]` selon la sémantique retenue) ; ajouter un test 503 puis 200 sur `download_eml`.

### [Moyenne] Extraction des pièces jointes : cas limites non couverts, dont un trou fonctionnel
- Emplacement : `backend/app/archive.py:270-305`, lignes non couvertes 245-246, 288-292, 299
- Problème : seuls un `multipart/mixed` classique et un message simple sont testés. Non couverts : (1) **message mono-partie dont la racine est la pièce jointe** (`Content-Type: application/pdf` + `Content-Disposition: attachment` à la racine, cas de certains scanners/applications) — vérifié : `extract_attachment_files` renvoie `[]`, la PJ n'est pas extraite à cause de `not is_root` ; (2) nom > 100 caractères (troncature avec/sans extension) ; (3) nom sans extension en double (`_unique_name`, l. 299) ; (4) noms RFC 2047/2231 non ASCII (vérifié : `=?utf-8?b?…?=` → `été.pdf`, correct mais non figé) ; (5) MIME invalide déclenchant l'exception l. 245 (et le libellé « pièces jointes non extraites ») ; (6) noms réservés Windows (`CON`, `NUL` → conservés tels quels) et noms commençant par un point (`.htaccess` en double → ` (2).htaccess`) ; (7) `multipart/alternative` imbriqué et image inline nommée (comportement « partie nommée = PJ » décidé l. 278 mais non testé).
- Recommandation : un fichier `tests/test_archive.py` de tests unitaires purs sur `extract_attachment_files`, `attachment_file_name` et `_unique_name`, paramétrés sur ces cas ; décider explicitement du cas « racine = PJ » (et corriger si on veut l'extraire).

### [Moyenne] Nommage des `.eml` : troncature et cas limites non vérifiés
- Emplacement : `backend/app/archive.py:131-138`, `backend/app/config.py:37-40`
- Problème : dans tous les tests, le sujet est assez court pour ne jamais être tronqué (`max_file_name_length=50`) : la règle de troncature, centrale pour la compatibilité avec Archivage, n'est vérifiée par aucune assertion. Vérifié : un identifiant long (ex. élément partagé `uuid:id`) produit un nom de 74 caractères pour une limite de 50 (`available` tombe à 0 mais le suffixe n'est pas borné). Le bornage 50-180 de `clamp_file_name_length` n'est pas testé, ni les dates < 1980 (l. 317), ni un sujet vide.
- Recommandation : tests unitaires paramétrés de `eml_file_name` : sujet 200 caractères → longueur exactement `max_length` ; sujet vide → `yyyyMMdd__id.eml` ; id contenant `:` → sanitisé ; id très long → documenter la longueur attendue ; `Settings(max_file_name_length=10)` → 50 et `=999` → 180 ; `date_ms` négatif → entrée datée 1980-01-01 dans le ZIP.

### [Moyenne] Garde-fous de sécurité corrects mais non verrouillés par des tests
- Emplacement : `backend/app/archive.py:120-128`, `:283-293`, `backend/app/main.py:271-276` et `:327`, `backend/tests/test_api.py:312`
- Problème : ce point a été signalé par le teammate « security ». J'ai vérifié par script que le code actuel se comporte correctement :
  - dossier `/../../etc` → `_/_/etc`, et `/./a/..` → `_/a/_` ;
  - pièce jointe nommée `..` → `_` ; `..\..\x`, `/etc/x` et `C:\x` → `x` ; `a\r\nb` → `a__b` ;
  - nom de dossier `a"b\r\nSet-Cookie: x` → `a_b_Set-Cookie_x_<date>.zip`, et `Été` → `Ete_<date>.zip`, donc pas d'injection dans `Content-Disposition`.

  Pourtant, le seul test sur ces chemins est `filename="../devis.pdf"`. Les gardes contre le Zip Slip (`sanitize_path_component`, `attachment_file_name`) et l'assainissement de `archive_file_name` pourraient régresser sans qu'aucun test échoue. Aucun test ne vérifie non plus que le logout invalide le jeton côté Zimbra, ce qui n'est pas le cas aujourd'hui (voir le rapport security).
- Recommandation : ajouter des tests paramétrés qui figent ces sorties. Exemple d'invariant : pour toute entrée, aucun composant de `namelist()` ne vaut `..` ou `.`, et aucun nom ne commence par `/` ni ne contient `\` ou `:`.
  ```python
  @pytest.mark.parametrize("nom, attendu", [
      ("..", "_"), ("..\\..\\x", "x"), ("/etc/x", "x"), ("C:\\x", "x"), ("a\r\nb", "a__b"),
  ])
  def test_attachment_file_name_reste_dans_le_dossier(nom, attendu):
      assert attachment_file_name(nom, "d") == attendu
  ```
  Ajouter au faux serveur un dossier `absFolderPath="/../x"` et vérifier les noms de l'archive. Ajouter un test API avec un dossier nommé `a"b\r\nc` qui vérifie que l'en-tête `Content-Disposition` reste une seule valeur ASCII entre guillemets.

### [Moyenne] Aucun test frontend, notamment du proxy Next.js
- Emplacement : `frontend/app/api/[...path]/route.ts:1-62`, `frontend/package.json:5-10`
- Problème : aucun framework de test ; `npm run lint` n'est que `tsc --noEmit`. Le proxy porte pourtant une logique critique non testée : filtrage des en-têtes hop-by-hop, conservation de plusieurs `Set-Cookie`, retransmission en flux (pas de mise en tampon du ZIP), propagation de l'annulation (`signal`) vers le backend, réponse 502 « Service d'archivage injoignable », encodage des segments de chemin (`..`, `%2F`). Côté UI : construction de l'URL d'export et du JSON de filtre (`lib/api.ts`), suivi de l'avancement, éditeur ET/OU.
- Recommandation : ajouter Vitest (environnement `node`) pour `route.ts` en mockant `fetch` : (a) en-têtes exclus absents, `Set-Cookie` multiples préservés ; (b) `fetch` rejeté → 502 JSON ; (c) le `body` renvoyé est le même `ReadableStream` ; (d) `path=["..","x"]` ne sort pas de `/api/`. Puis Testing Library pour `FilterEditor` (sérialisation identique au modèle `MessageFilter`) et, à terme, un test e2e Playwright login → export contre le backend + `FakeZimbra`.

### [Faible] Exceptions inattendues pendant le flux et interruption du téléchargement non testées
- Emplacement : `backend/app/main.py:279-332`, `backend/app/archive.py:193-211`
- Problème : une exception non `ZimbraError` en cours de génération (ex. `RuntimeError` de `zipfile` pour une entrée > 2 Gio sans `force_zip64`, bug de parsing) interrompt la réponse après l'envoi des en-têtes 200 : archive tronquée côté client. Aucun test ne vérifie que `progress.finished` passe à `True` et que le client Zimbra est fermé quand le générateur est fermé prématurément (client qui annule), ni le comportement côté HTTP.
- Recommandation : test unitaire appelant `export(...)` (ou le générateur `content()`), lisant un chunk puis appelant `.close()` : vérifier `registry.get(...).finished is True` et que le transport a été fermé ; test avec un transport levant `RuntimeError` au 2e message.

### [Faible] Cas limites d'export non couverts
- Emplacement : `backend/app/archive.py:88-103`, `:150-157`, `backend/app/main.py:224-233`
- Problème : non testés : export de la racine avec `include_subfolders=false` (exporte `inid:1`, donc un ZIP vide de 22 octets — vérifié valide, mais résultat surprenant pour l'utilisateur) ; dossier sans aucun message (aucun test n'asserte qu'un ZIP vide reste valide) ; sous-dossiers de la Corbeille/Spam exclus de l'export de toute la boîte ; dossier non-mail (calendrier) placé sous un dossier mail ; deux dossiers Zimbra dont les noms se sanitisent pareil (`a:b` / `a?b`) ; noms de dossiers non ASCII dans le ZIP (drapeau UTF-8) ; `summary.total_size` jamais asserté ; réutilisation d'un même `export_id`.
- Recommandation : enrichir `ROOT` du faux (sous-dossier de Trash, calendrier sous Inbox, dossier « Été ») et ajouter les assertions correspondantes ; asserter `total_size` dans `test_summary_with_subfolders`.

### [Faible] Filtres : valeurs à risque pour la syntaxe de recherche non testées
- Emplacement : `backend/app/filters.py:16-30`, `backend/tests/test_filters.py:7-13`
- Problème : `SIMPLE_VALUE` accepte sans guillemets `*` seul, `-foo`, `+foo`, `a*b`, et des caractères Unicode (`\w`). Leur interprétation par Zimbra (joker global, négation, opérateur) n'est pas couverte ; seules quelques valeurs sont testées. Pas de test non plus pour `\\` (échappement), les parenthèses, `:` dans une valeur, la limite de 200 caractères ni les maxima de 10 groupes / 20 conditions.
- Recommandation : compléter `test_quote_value` (paramétré) avec `*`, `-foo`, `a:b`, `(x)`, `c:\\temp`, `AND` (majuscules), et `test_invalid_filters` avec 11 groupes, 21 conditions, valeur de 201 caractères ; décider si `*` seul et `-` initial doivent être cités.

### [Faible] Connexion : attributs du cookie et cas d'erreur partiellement vérifiés
- Emplacement : `backend/app/main.py:119-142`, `backend/tests/test_api.py:68-83`
- Problème : les tests vérifient `HttpOnly` et `Path=/api` mais pas `Secure` (la fixture force `cookie_secure=False`, la valeur par défaut `True` n'est jamais testée), `SameSite=lax`, ni `Max-Age` dérivé de `lifetime` (`172800000` → `172800`, et `lifetime=0` → cookie de session). Pas de test d'un corps de login invalide (422) ni de Zimbra injoignable à la connexion (502).
- Recommandation : un test avec `cookie_secure=True` assertant `Secure`, `SameSite=lax`, `Max-Age=172800` ; un test `lifetime` absent → pas de `Max-Age`.

### [Faible] Couleurs de dossiers et autocomplétion : branches d'erreur non couvertes
- Emplacement : `backend/app/zimbra.py:139-148`, `:304-315`
- Problème : `color` non entier ou hors palette, `rgb` mal formé (`#12ab9`), et entrée d'autocomplétion sans `@` (groupe de contacts) ne sont pas testés.
- Recommandation : tests unitaires paramétrés de `_folder_color` (`{"color": "x"}` → `None`, `{"color": 42}` → `None`, `{"rgb": "#12ab9", "color": 1}` → `#5B9BF2`) et un contact `{"email": "Équipe", "type": "group"}` dans le faux.

### [Info] Isolation des tests et dépendances de test
- Emplacement : `backend/tests/test_api.py:48-58`, `backend/tests/test_zimbra.py:8`, `backend/pytest.ini:1-2`, `backend/requirements-dev.txt:1-2`
- Problème : les tests modifient l'objet global `app.dependency_overrides` (nettoyé en fin de fixture, mais pas en cas d'échec de setup : pas de `try/finally`) ; `SETTINGS` de `test_zimbra.py` est construit à l'import du module (lecture d'environnement au moment de la collecte). `pytest.ini` n'active ni `--strict-markers` ni `filterwarnings = error` : l'avertissement de dépréciation Starlette/httpx (`install httpx2 instead`) annonce une casse future de `TestClient` qui passera inaperçue. Tests rapides (0,7 s) et déterministes par ailleurs (pas de réseau réel, `sleep` injecté, horloge injectée dans `ProgressRegistry`, pas d'assertion sur la date du jour) — bon point.
- Recommandation : fixture `client` avec `try/finally`, `SETTINGS` dans une fixture, `filterwarnings = error` avec exceptions ciblées, `pytest-cov` dans les dépendances de dev, aligner la version Python locale/CI (3.13).

### [Info] Points positifs
- Faux serveur Zimbra au niveau transport (`httpx.MockTransport`) plutôt que des mocks de méthodes : le code réel de sérialisation SOAP, des cookies, de la pagination et du streaming est exercé.
- `QueryParser` du faux vérifie que la requête est bien formée (parenthèses, précédence ET/OU) : bonne protection contre les régressions de `filters.py`.
- Injection de `sleep` et de `clock` : tests de retries et d'expiration de l'avancement sans attente réelle.
- Tests de bout en bout sur le contenu réel du ZIP (noms, contenu des `.eml`, `erreurs.txt`, PJ dupliquées, traversée `../devis.pdf`).
