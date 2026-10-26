# Revue de code — Synthèse

Périmètre : dépôt entier (backend FastAPI, frontend Next.js, chart Helm, Docker, CI). Revue en lecture seule, quatre axes.

## Résumé exécutif

Le socle est sain. Le code est court, testable, avec des tests rapides et une couverture backend de 96 %. Côté sécurité, le cookie est HttpOnly+Secure, il n'y a pas de SSRF ni de zip-slip, le TLS est vérifié et les conteneurs tournent sans root. Les risques majeurs sont de deux natures. D'abord, la robustesse d'un export : une session expirée produit un ZIP partiel avec un statut 200, il n'y a aucune journalisation, et rien ne limite la concurrence ni les ressources (DoS authentifié). Ensuite, l'absence de garde-fous : aucune CI sur push ou PR (les tests ne tournent qu'au tag), aucun test frontend, pas de rate limiting sur le login. Côté performance, le gain principal est de ne plus parcourir deux fois la boîte (estimation + export) et de paralléliser les téléchargements, aujourd'hui séquentiels.

## Tableau des findings (trié par sévérité puis impact)

| Sévérité | Axe | Emplacement | Problème | Effort |
|---|---|---|---|---|
| Élevée | tests / quality | archive.py:207-208, 232-235 | `SessionExpired` avalée dans `erreurs.txt` : ZIP partiel livré en 200, non testé | S |
| Élevée | quality | backend/app/ (global) | Aucune journalisation backend | S |
| Élevée | tests / quality / security | .github/workflows/release.yml | Pas de CI sur push/PR (tests, lint, `helm lint`) | S |
| Élevée | tests | frontend/ | Aucun test frontend ; `lint` = `tsc --noEmit` | M |
| Élevée | security / perf | main.py:243-332, archive.py:160-250, backend.yaml:51-54 | Aucune limite de concurrence/ressources sur export et estimation ; `/tmp` sans sizeLimit | M |
| Élevée | security | main.py:119-142 | Pas de rate limiting sur le login (oracle de mots de passe, verrouillage de comptes) | M |
| Élevée | perf | page.tsx:139-145, archive.py:150-157 | Le téléchargement lance un second parcours complet (estimation) | M |
| Élevée | perf | archive.py:97-103,141-157, zimbra.py:317-326 | Une recherche par dossier, offset profond, compteurs de `GetFolderRequest` non exploités | M |
| Élevée | perf | archive.py:197-206,226-239 | Téléchargement des `.eml` strictement séquentiel | M |
| Élevée | quality | page.tsx:128-152, api.ts:133-134 | `window.location.assign` : sur erreur HTTP, JSON brut affiché et sortie de l'app | S |
| Moyenne | security | main.py:133-147, zimbra.py:238-252 | Jeton Zimbra non invalidé au logout, cookie persistant | S |
| Moyenne | security | zimbra.py:195-196, main.py:61-65 | Erreurs internes (hôtes, TLS, Fault SOAP) renvoyées au client | S |
| Moyenne | security | next.config.ts:1-9 | En-têtes de sécurité HTTP absents (CSP, XFO, nosniff, HSTS) | S |
| Moyenne | security | frontend/app/api/[...path]/route.ts:33-44 | Proxy : corps bufferisé sans limite, segments `..` non filtrés (à vérifier) | S |
| Moyenne | security | main.py:119-142,279-288 | SameSite=Lax + export en GET ; login CSRF | S |
| Moyenne | security | charts/…/backend.yaml, frontend.yaml, values.yaml | Pas de NetworkPolicy, token ServiceAccount monté | S |
| Moyenne | security | release.yml:14-16,25,27,36… | Permissions d'écriture globales, actions non épinglées par SHA, pas de scan/provenance | S |
| Moyenne | security | requirements.txt, Dockerfile | Pas de scan de dépendances, transitives non verrouillées | S |
| Moyenne | perf / quality | zimbra.py:188-200 | Retries bloquants jusqu'à ~6 min, sans gigue, pas de retry sur erreurs de transport | S |
| Moyenne | perf | archive.py:196-208 | Une erreur de page de recherche abandonne le reste du dossier | S |
| Moyenne | perf / security | archive.py:244,253-267 | Pièces jointes décodées entièrement en mémoire | M |
| Moyenne | perf | archive.py:193,308-311 | Deflate systématique, même sur contenus déjà compressés | S |
| Moyenne | perf | main.py:32-33, zimbra.py:173-177 | Un `httpx.Client` par requête (pas de réutilisation TLS) | S |
| Moyenne | perf | main.py:114-116 | `/api/health` dans le threadpool partagé : risque de kill du pod sous charge | S |
| Moyenne | quality | main.py (global), zimbra.py:258-283 | `main.py` trop gros, erreurs hétérogènes, `KeyError` bruts → 500 | M |
| Moyenne | quality | page.tsx:45-324 | `FoldersPage` : 280 lignes, 12 états, effets de dérivation | M |
| Moyenne | quality | api.ts:1-64 | Contrat d'API recopié à la main, casts, sans validation | M |
| Moyenne | tests | zimbra.py, filters.py, archive.py | Retries, `DateRange`, `quote_value`, pièces jointes, cookie, progression peu testés | M |
| Faible | tous | voir détail par axe | ~40 constats mineurs (Helm, Docker, accessibilité, doc, dérive de config…) | — |

## Top 5 des actions prioritaires

1. **Ajouter une CI sur push/PR** (pytest, `tsc`, lint, `helm lint`, scan de dépendances) — axes tests/security/quality ; gain : filet de sécurité pour toutes les corrections suivantes. Effort S.
2. **Fiabiliser l'export** : propager `SessionExpired` (erreur explicite plutôt que ZIP partiel en 200), ajouter `logging`, et corriger le téléchargement frontend (`fetch` + gestion d'erreur) — archive.py:207-208, main.py, page.tsx:128-152. Effort S.
3. **Limiter la charge** : limite de concurrence d'exports par utilisateur et global, `sizeLimit` sur `/tmp`, requests/limits Helm, rate limiting login (ingress ou middleware) — main.py:243-332, backend.yaml. Effort M.
4. **Supprimer le double parcours et paralléliser** : réutiliser les compteurs de `GetFolderRequest` pour l'estimation, un seul `SearchRequest` par dossier, téléchargements `.eml` concurrents bornés — archive.py:141-239. Gain : temps d'export divisé plusieurs fois. Effort M.
5. **Durcir l'exposition HTTP** : en-têtes de sécurité Next, filtrage des segments `..` et limite de taille dans le proxy, NetworkPolicy, logout qui invalide le jeton (`EndSessionRequest`) — next.config.ts, route.ts, main.py:133-147. Effort S–M.

## Détail par axe

- [review/perf.md](perf.md) — 3 Élevées (double parcours, une recherche par dossier, téléchargement séquentiel), 8 Moyennes, reste Faible/Info ; gains principaux côté appels Zimbra.
- [review/quality.md](quality.md) — 2 Élevées (aucun log, téléchargement frontend fragile), gestion d'erreurs hétérogène, `main.py` et `FoldersPage` à découper, contrat d'API dupliqué.
- [review/tests.md](tests.md) — 49 tests verts, 96 % de couverture backend ; frontend à 0 %, aucune CI, cas d'erreur et de sécurité à compléter.
- [review/security.md](security.md) — socle sain ; 2 Élevées (DoS authentifié, absence de rate limiting login), 8 Moyennes (logout, fuite d'erreurs, en-têtes, proxy, CSRF, Helm, supply chain, dépendances).

## Couverture de tests

Mesurée avec `pytest-cov --cov-branch` (absent de `requirements-dev.txt`) : **96 %** global (671 instructions, 24 manquées ; 128 branches, 5 partielles).

| Module | Couverture |
|---|---|
| archive.py | 94 % |
| config.py | 96 % |
| filters.py | 100 % |
| main.py | 98 % |
| progress.py | 98 % |
| suggestions.py | 100 % |
| zimbra.py | 96 % |
| frontend | 0 % (aucun test) |

## Réserves

- Le comportement réel du proxy Next face à un segment `..` n'a pas été testé.
- Les CVE des versions de dépendances n'ont pas été vérifiées.
- Le rapport qualité n'a pas exécuté le code ; les numéros de ligne proviennent de la lecture des fichiers. Certains numéros de ligne de `perf.md` (archive.py, zimbra.py, main.py, DownloadOverlay.tsx, page.tsx, ConditionValueInput.tsx) n'ont pas été revérifiés.
- Les chevauchements entre axes (concurrence/DoS, `SessionExpired`, absence de CI, retries) sont consolidés dans le tableau ci-dessus ; aucun échange direct entre teammates n'a eu lieu.
