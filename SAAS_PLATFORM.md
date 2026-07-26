# Plateforme SaaS Dograh

Cette copie du dépôt ajoute une couche multi-clients au-dessus de Dograh : un
catalogue d'offres, un abonnement par organisation, des quotas appliqués au
moment de l'exécution, et un tableau de bord d'administration réservé aux
superusers. Le reste du produit (workflows, téléphonie, WhatsApp, campagnes)
est inchangé.

Ce document décrit uniquement ce qui est implémenté dans cette copie.

## Sommaire

- [Ce qui a été ajouté](#ce-qui-a-été-ajouté)
- [Modèle de données](#modèle-de-données)
- [Plans par défaut](#plans-par-défaut)
- [API](#api)
- [Application des quotas](#application-des-quotas)
- [Démarrer cette copie en parallèle de l'installation existante](#démarrer-cette-copie-en-parallèle-de-linstallation-existante)
- [Créer le premier administrateur plateforme](#créer-le-premier-administrateur-plateforme)
- [Créer un client](#créer-un-client)
- [Limites connues](#limites-connues)

## Ce qui a été ajouté

| Domaine | Emplacement |
| --- | --- |
| Tables et migration | `api/db/models.py`, `api/alembic/versions/*_add_subscription_tables.py` |
| Accès base de données | `api/db/subscription_client.py` (monté dans `api/db/db_client.py`) |
| Logique métier | `api/services/subscription/` (`plans.py`, `service.py`, `enforcement.py`) |
| Schémas Pydantic | `api/schemas/subscription.py` |
| API d'administration | `api/routes/platform_admin.py` (préfixe `/platform-admin`) |
| API client | `GET /organization/subscription` dans `api/routes/organization.py` |
| Tableau de bord | `ui/src/app/platform-admin/`, wrapper HTTP `ui/src/lib/platformAdminApi.ts` |
| Bootstrap | `scripts/bootstrap_platform_admin.py` |
| Stack parallèle | `docker-compose.saas.yaml`, `.env.saas.example` |

## Modèle de données

Trois tables sont ajoutées, plus deux colonnes sur `organizations`.

**`subscription_plans`** — le catalogue d'offres. Un plan porte son prix
(`price_amount`, `currency`, `billing_interval`), une durée d'essai
(`trial_days`), ses limites (`max_voice_minutes`, `max_whatsapp_messages`,
`max_workflows`, `max_campaigns_per_month`, `max_users`,
`max_concurrent_calls`) et une carte de fonctionnalités `features` (JSONB :
`voice`, `whatsapp`, `campaigns`, `telephony`, `api_access`,
`knowledge_base`, `integrations`). **Une limite à `NULL` signifie illimité.**

**`organization_subscriptions`** — au plus un abonnement par organisation
(contrainte d'unicité sur `organization_id`). Il porte le plan, le statut
(`trialing`, `active`, `past_due`, `suspended`, `cancelled`), la période en
cours (`current_period_start`, `current_period_end`), la fin d'essai
éventuelle, `cancel_at_period_end`, et `limit_overrides` (JSONB) qui permet de
surcharger une limite du plan pour ce client seulement.

**`subscription_events`** — le journal d'audit : `provisioned`,
`plan_changed`, `suspended`, `reactivated`, `cancelled`, `renewed`,
`limits_updated`, `quota_blocked`. Chaque entrée conserve l'auteur
(`actor_user_id`), une charge utile JSON et une note libre.

**`organizations`** reçoit `name` et `contact_email` : jusqu'ici une
organisation n'était identifiée que par son `provider_id`, ce qui ne suffit pas
pour une console d'administration.

Les **limites effectives** d'une organisation sont celles du plan, écrasées clé
par clé par `limit_overrides`. La **consommation** est calculée à la volée sur
la période en cours de l'abonnement : minutes vocales issues des exécutions de
workflow (hors canaux texte et WhatsApp), messages WhatsApp, nombre de
workflows, campagnes de la période et utilisateurs de l'organisation.

## Plans par défaut

`api/services/subscription/plans.py` contient un catalogue de départ, créé par
`ensure_default_plans()`. L'opération est idempotente et **ne modifie jamais un
plan existant** : un plan que l'administrateur a édité ou désactivé n'est pas
ressuscité au prochain lancement. Les tarifs ci-dessous sont des valeurs de
départ à ajuster depuis le tableau de bord.

| Code | Nom | Prix | Minutes vocales | Messages WhatsApp | Workflows | Campagnes / période | Utilisateurs | Appels simultanés |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `trial` | Essai (14 j) | 0 EUR | 60 | 200 | 3 | 1 | 3 | 2 |
| `starter` | Starter | 149 EUR | 500 | 2 000 | 10 | 5 | 10 | 5 |
| `business` | Business | 449 EUR | 2 000 | 10 000 | 50 | 25 | 30 | 15 |
| `enterprise` | Enterprise | 1 490 EUR | illimité | illimité | illimité | illimité | illimité | illimité |

Fonctionnalités : `trial` n'ouvre ni les campagnes, ni la téléphonie, ni l'accès
API ni les intégrations ; `starter` ajoute campagnes et téléphonie ; `business`
et `enterprise` activent tout.

## API

### Administration plateforme

Préfixe `/api/v1/platform-admin`, chaque route exige un superuser
(`Depends(get_superuser)`) :

| Méthode | Route | Rôle |
| --- | --- | --- |
| `GET` | `/plans` | catalogue |
| `POST` | `/plans` | créer un plan |
| `PATCH` | `/plans/{plan_id}` | modifier un plan |
| `DELETE` | `/plans/{plan_id}` | désactivation douce ; 409 si des organisations y sont abonnées |
| `GET` | `/metrics` | organisations, abonnements par statut, MRR, consommation de la période |
| `GET` | `/organizations` | liste paginée avec plan, statut, consommation et limites |
| `POST` | `/organizations` | crée organisation + utilisateur propriétaire + abonnement |
| `GET` | `/organizations/{org_id}` | détail : abonnement, plan, limites, usage, utilisateurs, 20 derniers événements |
| `POST` | `/organizations/{org_id}/subscription` | affecter / changer de plan |
| `POST` | `/organizations/{org_id}/suspend` | suspendre |
| `POST` | `/organizations/{org_id}/reactivate` | réactiver |
| `POST` | `/organizations/{org_id}/cancel` | résilier (immédiatement ou en fin de période) |
| `POST` | `/organizations/{org_id}/renew` | avancer la période d'un intervalle |
| `PATCH` | `/organizations/{org_id}/limits` | surcharges de limites |
| `GET` | `/organizations/{org_id}/usage` | consommation de la période |
| `GET` | `/organizations/{org_id}/events` | journal d'audit |

### Côté client

`GET /api/v1/organization/subscription` renvoie à l'organisation sélectionnée
son plan, son statut, sa période, ses limites effectives, sa consommation et
ses fonctionnalités.

### Tableau de bord

L'écran `/platform-admin` de l'interface Next.js consomme ces routes :
indicateurs globaux, liste des organisations avec barres de consommation,
fiche client (changement de plan, suspension, réactivation, résiliation,
renouvellement, surcharges de limites) et journal d'audit. Il est protégé côté
interface par un garde superuser, et côté serveur par `get_superuser` sur
chaque route.

## Application des quotas

Tout passe par `api/services/subscription/enforcement.py`, qui expose trois
vérifications renvoyant un `SubscriptionCheck(allowed, error_code, error_message)` :

- `check_run_allowed(organization_id)` — statut de l'abonnement, fonctionnalité
  `voice` et quota de minutes. Branché dans
  `api/services/quota_service.py::authorize_workflow_run_start`, c'est-à-dire le
  point de contrôle unique déjà utilisé par la signalisation WebRTC, la
  téléphonie, les agents publics, le chat texte, le streaming d'agent et les
  dispatchers de campagne.
- `check_feature_allowed(organization_id, feature)` — utilisé par
  `api/routes/campaign.py` pour la fonctionnalité `campaigns`.
- `check_whatsapp_message_allowed(organization_id, count=1)` — utilisé par le
  dispatcher de campagne WhatsApp, le service de conversation et la boîte de
  réception (reprise humaine).

Règles de comportement :

1. Si `SUBSCRIPTION_ENFORCEMENT_ENABLED` vaut `false`, toutes les vérifications
   autorisent.
2. Si l'organisation n'a **aucune** ligne `organization_subscriptions`, elles
   autorisent également. Les installations existantes et le mode OSS
   fonctionnent donc exactement comme avant.
3. Statut `suspended` ou `cancelled` : refus. `trialing`, `active` et `past_due`
   poursuivent les vérifications.
4. Période terminée (`current_period_end` dépassé) : refus. Le renouvellement
   n'est jamais silencieux ; il faut appeler `renew`.
5. Limite `NULL` : illimité. Limite atteinte ou dépassée : refus.
6. **Toute erreur inattendue autorise** (fail-open) et écrit un
   `logger.warning`. Un incident de la couche facturation ne doit jamais couper
   la production vocale d'un client ; la mesure de consommation continue, le
   dépassement reste donc visible ensuite par l'administrateur.

Chaque refus écrit un événement `quota_blocked` dans `subscription_events`, en
best-effort : le journal ne peut pas devenir un second mode de panne.

### Ce que voit l'utilisateur en cas de dépassement

Le refus remonte par le chemin de quota existant, avec un code stable et un
message en français : `subscription_suspended`, `subscription_cancelled`,
`subscription_expired`, `voice_quota_exceeded`, `whatsapp_quota_exceeded`,
`feature_not_in_plan`. Concrètement, l'appel ou le message n'est pas démarré :
la session WebRTC est refusée, l'appel téléphonique n'est pas lancé, l'envoi
WhatsApp est ignoré, la campagne ne démarre pas. Rien n'est facturé et rien
n'est supprimé — l'administrateur plateforme peut réactiver, changer de plan,
relever une limite ou renouveler la période, et le service repart immédiatement.

## Démarrer cette copie en parallèle de l'installation existante

`docker-compose.saas.yaml` est dérivé de `docker-compose.yaml` avec des noms de
projet, de conteneurs, de volumes et de réseau distincts (suffixe `saas`) et des
ports décalés. Les deux stacks ne partagent ni base de données, ni volume, ni
port.

| Service | Port hôte | Port conteneur |
| --- | --- | --- |
| ui | 3020 | 3010 |
| api | 8010 | 8000 |
| postgres | 5442 | 5432 |
| redis | 6389 | 6379 |
| minio | 9010 / 9011 (127.0.0.1) | 9000 / 9001 |
| cloudflared (profil `tunnel`) | 2010 | 2000 |

> L'interface est publiée sur **3020** et non 3010 : l'image UI de ce dépôt
> écoute sur 3010 à l'intérieur du conteneur, et le port hôte 3010 est déjà pris
> par l'installation lancée avec `docker-compose.yaml`.

```bash
cp .env.saas.example .env.saas
# éditer .env.saas : OSS_JWT_SECRET, mots de passe postgres/redis/minio
docker compose -f docker-compose.saas.yaml --env-file .env.saas up -d --build
```

`.gitignore` ne couvre que `.env`, `.env.prod` et `.env.test` : ajouter
`.env.saas` avant de committer, sinon les secrets de ce déploiement partent dans
l'historique.

L'interface est alors sur <http://localhost:3020> et l'API sur
<http://localhost:8010>. La première construction est longue (dépendances
Python, pipecat, ffmpeg, build Next.js) ; pour réutiliser une image déjà
construite, pointer `SAAS_API_IMAGE` / `SAAS_UI_IMAGE` sur ses tags dans
`.env.saas`.

Le point d'entrée de l'image api exécute `alembic upgrade head` au démarrage :
les tables d'abonnement sont créées au premier lancement, aucune commande de
migration manuelle n'est nécessaire.

Les services réservés au déploiement public de `docker-compose.yaml` (nginx,
coturn, dograh-init) ne sont volontairement pas repris : ils occupent les ports
80, 443 et 3478, qui ne peuvent pas être publiés deux fois sur la même machine.
Pour exposer cette copie sur Internet, utiliser `docker-compose.yaml` sur une
autre machine, ou le profil `tunnel` :

```bash
docker compose -f docker-compose.saas.yaml --env-file .env.saas --profile tunnel up -d
```

## Créer le premier administrateur plateforme

`scripts/bootstrap_platform_admin.py` promeut un utilisateur en superuser — en
créant le compte s'il n'existe pas encore, avec les mêmes primitives que
l'inscription (`api/routes/auth.py::signup`) — puis appelle
`ensure_default_plans()`. Le script est idempotent : le relancer sur une
installation déjà initialisée ne fait que rapporter l'état courant, et ne change
jamais le mot de passe d'un compte existant.

Dans la stack Docker (le script est monté dans le conteneur api) :

```bash
docker compose -f docker-compose.saas.yaml --env-file .env.saas exec api \
  python -m scripts.bootstrap_platform_admin --email admin@example.com --password 'mot-de-passe-fort'
```

Depuis l'hôte, avec l'environnement de l'api chargé (`DATABASE_URL` et
`REDIS_URL` doivent pointer sur les ports décalés 5442 et 6389) :

```bash
source venv/bin/activate && set -a && source api/.env && set +a
python -m scripts.bootstrap_platform_admin --email admin@example.com --password 'mot-de-passe-fort'
```

Le script refuse un mot de passe vide, et refuse de créer un compte si
`--password` n'est pas fourni. Une fois connecté avec ce compte, le tableau de
bord `/platform-admin` est accessible.

## Créer un client

Depuis le tableau de bord `/platform-admin`, ou directement en API :

```bash
curl -X POST http://localhost:8010/api/v1/platform-admin/organizations \
  -H "Authorization: Bearer <jeton-du-superuser>" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "Client Démo",
        "contact_email": "contact@client-demo.fr",
        "owner_email": "proprietaire@client-demo.fr",
        "owner_password": "mot-de-passe-initial",
        "plan_code": "starter",
        "trial_days": 14
      }'
```

L'appel crée en une fois l'organisation, son utilisateur propriétaire (mot de
passe haché avec `api.utils.auth.hash_password`) et l'abonnement au plan
demandé, puis journalise un événement `provisioned`. Le propriétaire se connecte
ensuite normalement sur l'interface avec ces identifiants et voit son plan et sa
consommation.

Garder `ENABLE_SIGNUP=false` : sur une installation SaaS, les comptes sont créés
par l'administrateur plateforme, pas depuis la page de connexion.

## Limites connues

- **Aucun paiement en ligne.** Il n'y a pas d'intégration Stripe ou autre
  prestataire, pas de tokenisation de carte, pas de portail client. Le champ
  `external_reference` de l'abonnement est prévu pour y noter une référence
  externe, rien de plus.
- **Facturation non automatisée.** Aucune facture n'est générée ni envoyée, il
  n'y a pas de relance ni de calcul de dépassement à facturer. Le statut
  `past_due` doit être posé à la main par l'administrateur et n'a aucun effet
  bloquant.
- **Renouvellement manuel.** La période ne se prolonge pas toute seule : passé
  `current_period_end`, les exécutions sont refusées jusqu'à un appel explicite
  à `renew` (ou un changement de période via l'API d'abonnement). Aucune tâche
  planifiée ne renouvelle les périodes.
- **Trois limites sur six sont bloquantes** : `max_voice_minutes` (appels),
  `max_whatsapp_messages` (conversations, voir ci-dessous) et
  `max_campaigns_per_month` (création de campagne, HTTP 402). `max_workflows`,
  `max_users` et `max_concurrent_calls` sont stockées, affichées et suivies,
  mais ne refusent rien à l'exécution — l'interface le signale sous chaque
  champ.
- **`max_whatsapp_messages` compte des conversations, pas des messages.**
  Dograh ne persiste aucune ligne par message : le compteur mesure les fenêtres
  de conversation ouvertes sur la période, ce qui est aussi l'unité facturée par
  Meta. L'interface affiche « Conversations WhatsApp » pour éviter toute
  ambiguïté commerciale ; le nom de colonne, lui, reste historique.
- **Le quota vocal ne s'applique qu'aux canaux vocaux.** Les exécutions WhatsApp
  et chat texte passent par le même point d'autorisation mais sont contrôlées
  sur la fonctionnalité `whatsapp`, jamais sur les minutes : un client dont les
  minutes sont épuisées garde ses conversations WhatsApp actives.
- **Fail-open assumé.** En cas d'erreur inattendue dans la couche abonnement,
  l'exécution est autorisée. Un dépassement peut donc passer pendant un incident
  base de données ; il reste visible dans la consommation après coup.
- **Rétro-compatibilité.** Une organisation sans ligne d'abonnement n'est jamais
  bloquée. Tant qu'un client n'a pas été provisionné, il consomme sans limite.
- **Aucune notification.** Ni e-mail ni webhook lors d'une suspension, d'une fin
  d'essai ou d'un dépassement de quota ; tout se lit dans le tableau de bord et
  dans `subscription_events`.
- **Consommation recalculée à la demande.** Les compteurs sont agrégés au moment
  de la lecture ou de la vérification, sur la période de l'abonnement. Il n'y a
  pas de compteur pré-agrégé ni d'historique figé par période close.
