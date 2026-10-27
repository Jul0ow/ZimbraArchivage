# Revue de code — Synthèse

Revue read-only du dépôt complet (backend FastAPI, frontend Next.js, chart Helm, docker-compose, CI),
menée par quatre teammates en parallèle : `perf`, `quality`, `tests` et `security`.
Aucun fichier source n'a été modifié. Seul le dossier `review/` a été créé.

## Résumé exécutif

L'application est saine sur le plan sécurité. Aucune faille ne permet de lire les mails d'un autre
utilisateur. Les chemins écrits dans le ZIP sont correctement assainis, il n'y a ni SSRF ni proxy
ouvert, et les conteneurs tournent sans root. L'archive est générée en flux de bout en bout, ce qui
est le bon choix pour des boîtes de plusieurs Go.

Le risque majeur est la **complétude de l'archive**. Un jeton expiré en cours d'export produit un
ZIP « réussi » qui ne contient que `erreurs.txt`, et la pagination par offset peut dupliquer ou
omettre des messages, sans que l'utilisateur soit averti.

Viennent ensuite deux risques de **déni de service** : les exports simultanés ne sont pas plafonnés,
et l'extraction des pièces jointes se fait entièrement en RAM avec une limite de 512 Mi.

La couverture backend est élevée (96 %), mais le faux Zimbra est trop permissif, la CI ne lance les
tests qu'au tag de release, et le frontend n'a aucun test.

## Tableau des findings (consolidé, trié par sévérité puis impact)

Les constats remontés par plusieurs teammates ne figurent qu'une fois. La colonne « Axe » liste
tous les axes concernés. Quand deux teammates n'étaient pas d'accord sur la sévérité, celle retenue
est justifiée en note.

| Sévérité | Axe | Emplacement | Problème | Effort |
|---|---|---|---|---|
| Élevée | tests, quality | `backend/app/archive.py:207-208`, `:231-232` | Session expirée avalée pendant l'export : HTTP 200 et ZIP ne contenant que `erreurs.txt` ¹ | S |
| Élevée | perf, quality | `backend/app/zimbra.py:263-284`, `:317-326` | Pagination `SearchRequest` par offset entrelacée avec les téléchargements : doublons ou messages manquants si la boîte change pendant l'export ² | M |
| Élevée | security, perf | `backend/app/main.py:279-332`, `:114-116`, `backend/app/zimbra.py:188-200` | Exports simultanés illimités sur le pool de 40 threads, `/api/health` synchrone sur ce même pool : la sonde échoue et le pod redémarre en coupant tous les exports | M |
| Élevée | security, perf | `backend/app/archive.py:240-267`, `values.yaml:31-33` | Extraction des pièces jointes : arbre MIME et pièces jointes décodées en RAM, OOM (512 Mi) déclenchable par un expéditeur tiers | M |
| Élevée | tests, quality | `.github/workflows/release.yml:5-12` | Tests, tsc et lint jamais lancés sur push ou PR (seulement au tag `v*`), aucun linter configuré | S |
| Élevée | tests | `backend/tests/fake_zimbra.py:176-189` | Le faux Zimbra fait « contient » par sous-chaîne alors que Zimbra cherche par mots : des filtres inopérants en production passent les tests | M |
| Élevée | perf | `backend/app/archive.py:194-206` | Téléchargements strictement séquentiels : 3 à 6 h pour 100 000 messages | M |
| Moyenne | security | `backend/app/main.py:145-147` | Logout sans `EndSessionRequest` : jeton Zimbra valide 2 jours | S |
| Moyenne | security | `backend/app/main.py:133-141` | Cookie de session = `ZM_AUTH_TOKEN` brut (accès complet à la messagerie s'il fuit) | M |
| Moyenne | security | `backend/app/main.py:119-142` | Aucune limite de tentatives au login : relais de brute force, IP du backend bannissable | S |
| Moyenne | security | `docker-compose.yml:16`, `:32-33` | Publication HTTP sur 0.0.0.0 avec `COOKIE_SECURE=false` | S |
| Moyenne | security | `charts/zimbra-archivage/values.yaml:71-83`, `frontend/next.config.ts` | Ingress sans TLS par défaut, aucun en-tête de sécurité (CSP, HSTS, frame-ancestors) | S |
| Moyenne | perf | `frontend/app/dossiers/page.tsx:139-146`, `backend/app/archive.py:150-157` | Recherche complète faite deux fois (estimation puis export), parfois en parallèle | S |
| Moyenne | perf | `backend/app/main.py:32-33`, `backend/app/zimbra.py:164-177` | Nouveau `httpx.Client` et nouvelle connexion TLS à chaque requête | S |
| Moyenne | perf | `frontend/app/api/[...path]/route.ts:41-49` | Délais par défaut d'undici (300 s) dans le proxy : estimations longues coupées, ZIP tronqué possible (docker-compose seulement : dans Helm, l'ingress envoie `/api` directement au backend) | S |
| Moyenne | perf, quality, tests | `backend/app/zimbra.py:22`, `:188-200` | Retries : 6 envois au lieu des 5 annoncés, backoff `3**n` (363 s cumulés), erreurs de connexion jamais retentées, comportement non testé | S |
| Moyenne | tests | `backend/tests/test_api.py:19-20`, `backend/app/archive.py:50-68` | Fuseau horaire non vérifié (5 échecs avec `Asia/Tokyo`), `after:`/`before:` ignorés par le faux | S |
| Moyenne | tests | `backend/app/archive.py:279` | Pièce jointe non extraite quand le message entier est la pièce jointe (scanners) | S |
| Moyenne | tests | `backend/app/archive.py:131-138` | Suffixe `_<id>.eml` non limité : 74 caractères pour une limite de 50 | S |
| Moyenne | tests, security | `backend/app/archive.py:120-128`, `:283-293`, `main.py:271-276` | Protections Zip Slip et Content-Disposition correctes mais non verrouillées par des tests | S |
| Moyenne | tests | `backend/app/archive.py:224-234`, `main.py:61-65` | Coupure réseau en cours de téléchargement et handler 502 jamais exercés | S |
| Moyenne | tests | `frontend/` | Aucun test frontend, dont le proxy Next.js | M |
| Moyenne | quality | `backend/app/archive.py:34-37`, `frontend/app/dossiers/page.tsx:26-43` | Règles « toute la boîte » (racine, Corbeille, Spam) dupliquées backend et frontend | S |
| Moyenne | quality | `backend/app/filters.py:22-56`, `frontend/lib/api.ts:18-35` | Contrat du filtre recopié à la main en TS, limites non reprises, absent d'OpenAPI | M |
| Moyenne | quality | `frontend/app/dossiers/page.tsx:45-324` | Composant de ~280 lignes, 13 `useState`, état dérivé synchronisé par effet | M |
| Moyenne | quality | `backend/app/` | Aucune journalisation backend | S |
| Moyenne | quality | `charts/zimbra-archivage/values.yaml:9-19` | Pas de `values.schema.json` : valeur invalide découverte au runtime (CrashLoop) | S |
| Faible | quality | `backend/app/archive.py:88-90` | `folder_id=1` sans sous-dossiers : ZIP vide sans erreur | S |
| Faible | quality | `.github/workflows/release.yml:76`, `:137` | `:latest` poussé sur une prerelease, alors que `docker-compose.yml` l'utilise par défaut | S |
| Faible | security | `.github/workflows/release.yml`, Dockerfiles | Actions et images non épinglées, `GITHUB_TOKEN` en écriture sur tous les jobs | S |
| Faible | security | `charts/zimbra-archivage/templates/*.yaml` | Pas de NetworkPolicy, ServiceAccount non restreint | S |
| Faible | security, quality | `backend/app/main.py:61-65`, `archive.py:234` | Messages d'erreur httpx et Zimbra relayés tels quels au client | S |
| Faible | divers | voir rapports | 21 autres constats faibles : CSRF « de charge » via GET, registre de progression non borné, Swagger exposé, recompression des PJ, `/tmp` non dimensionné, duplications diverses, casts TS, réponses Zimbra mal formées → 500… | — |

¹ `quality` classait ce point en Moyenne. Il est retenu en **Élevée** : pour un outil d'archivage,
une archive vide présentée comme réussie est le pire résultat possible.
² `perf` classait d'abord ce point en Moyenne, sous l'angle du coût, puis l'a remonté en **Élevée**
pour la même raison : la complétude de l'archive n'est pas garantie et rien ne le signale. La page
suivante n'est demandée qu'après environ 500 téléchargements, donc plusieurs minutes plus tard.

Les constats « Info » (14 au total) sont dans les rapports par axe. Ils comprennent les vérifications
de sécurité négatives : pas de Zip Slip, pas d'IDOR, pas de SSRF, pas de proxy ouvert,
`npm audit` à 0.

## Top 5 des actions prioritaires

1. **Rendre la session expirée fatale pendant l'export** (tests, quality — `archive.py:207`, `:231`,
   mapper 401/403 en `SessionExpired` dans `download_eml`). Gain : plus d'archive vide présentée
   comme réussie. Effort S, avec un test de régression.
2. **Fiabiliser l'énumération des messages** (perf, quality — `zimbra.py:263-326`) : collecter
   d'abord tous les ids, ou trier explicitement et dédupliquer, avant de télécharger. Gain : archive
   complète et sans doublon. Effort M.
3. **Plafonner les exports simultanés et isoler `/api/health`** (security, perf — `main.py`) :
   sémaphore global et par session, `/api/health` en `async def`, retries plus courts. Gain : plus de
   redémarrage du pod sous charge. Effort M.
4. **Borner la mémoire de l'extraction des pièces jointes** (security, perf — `archive.py:240-267`) :
   taille maximale par message traité en RAM, au-delà spooler sur disque ou ignorer l'extraction en
   le signalant dans `erreurs.txt`. Gain : plus d'OOM déclenchable par un tiers. Effort M.
5. **CI sur push et PR** (tests, quality — `release.yml`) : pytest avec seuil de couverture, `tsc`,
   ruff, `helm lint`. Rendre aussi le faux Zimbra fidèle (recherche par mots, `after:`/`before:`).
   Gain : les correctifs 1 à 4 restent protégés. Effort S pour la CI, M pour le faux Zimbra.

Juste après : le logout avec `EndSessionRequest` et la limite de tentatives au login (S chacun).

## Détail par axe

- [`review/perf.md`](perf.md) — 3 Élevées, 5 Moyennes, 4 Faibles, 2 Info. ZIP bien généré en flux,
  et le proxy Next.js relaie le ZIP en flux sans le stocker. Points faibles : téléchargements
  séquentiels, PJ en RAM, pagination par offset, pool de threads partagé.
- [`review/quality.md`](quality.md) — 7 Moyennes, 12 Faibles, 4 Info. Duplication backend/frontend,
  page `dossiers` trop chargée, pas de logs ni de CI hors release. `tsc` et `helm lint` non exécutés
  (outils absents).
- [`review/tests.md`](tests.md) — 3 Élevées, 9 Moyennes, 5 Faibles, 2 Info. Bonne couverture
  apparente, mais faux Zimbra trop permissif et chemins d'erreur non vérifiés.
- [`review/security.md`](security.md) — 2 Élevées, 5 Moyennes, 6 Faibles, 6 Info. Pas de faille
  d'accès aux données. DoS, gestion du jeton et durcissement HTTP/TLS à traiter. `pip-audit` non
  exécuté (outil absent).

## Couverture de tests

Mesurée par `tests` avec `pytest --cov=app --cov-branch`, sous Python 3.14 (la CI utilise la 3.13).
49 tests passés sur 49.

| Module | Couverture (lignes + branches) |
|---|---:|
| `app/archive.py` | 94 % |
| `app/zimbra.py` | 95 % |
| `app/config.py` | 96 % |
| `app/main.py` | 98 % |
| `app/progress.py` | 98 % |
| `app/filters.py` | 100 % |
| `app/suggestions.py` | 100 % |
| **Backend total** | **96 %** (625 instructions, 128 branches) |
| `frontend/` | 0 % (aucun test) |

Les lignes non couvertes sont précisément les chemins d'erreur : session expirée en cours d'export,
coupure réseau, handler 502 et MIME invalide.
