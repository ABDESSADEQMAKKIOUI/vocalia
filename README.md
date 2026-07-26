# Vocalia

Plateforme d'agents conversationnels IA en marque blanche : voix, WhatsApp et web,
livrée en SaaS multi-clients avec une console d'administration.

Vocalia s'appuie sur [Dograh](https://github.com/dograh-hq/dograh) (BSD 2-Clause) et y
ajoute deux couches propres à ce dépôt :

- **Un canal WhatsApp complet** (API Meta Cloud) : conversations pilotées par l'agent,
  constructeur de modèles de messages avec soumission à l'approbation Meta, campagnes en
  masse depuis un CSV, connexion multi-comptes par Embedded Signup, et une boîte de
  réception avec reprise humaine.
- **Une couche SaaS multi-clients** : catalogue d'offres, abonnement par organisation,
  quotas appliqués à l'exécution et console d'administration de la plateforme.

## Ce que contient la couche SaaS

| Brique | Détail |
| --- | --- |
| Catalogue d'offres | Plans avec tarif, période, essai, limites et fonctionnalités |
| Abonnements | Un abonnement par organisation cliente, avec surcharges de limites par client |
| Quotas appliqués | Minutes vocales, conversations WhatsApp, campagnes par période |
| Fonctionnalités | Voix, WhatsApp, campagnes, téléphonie, accès API, base de connaissances, intégrations |
| Console d'administration | Tableau de bord, provisionnement d'un client, suspension, changement d'offre, renouvellement |
| Journal d'audit | Chaque mouvement d'abonnement et chaque blocage de quota est tracé |

La documentation complète de cette couche — modèle de données, règles d'application des
quotas, limites connues — est dans **[SAAS_PLATFORM.md](SAAS_PLATFORM.md)**.

## Démarrage

```bash
cp .env.saas.example .env.saas
docker compose --env-file .env.saas -f docker-compose.saas.yaml up -d
```

Créer le premier administrateur de la plateforme et le catalogue d'offres par défaut :

```bash
docker compose --env-file .env.saas -f docker-compose.saas.yaml exec api \
  python -m scripts.bootstrap_platform_admin --email vous@exemple.com --password '<mot-de-passe>'
```

La console d'administration est ensuite disponible sur `http://localhost:3020/platform-admin`.

Les ports de cette pile sont décalés (UI 3020, API 8010, PostgreSQL 5442, Redis 6389) afin
de cohabiter avec un déploiement Dograh standard sur la même machine.

## Origine et licence

Ce dépôt est un dérivé de Dograh, publié par Zansat Technologies Private Limited sous
licence BSD 2-Clause. La licence d'origine est conservée dans [LICENSE](LICENSE) et le
README amont dans [README.dograh.md](README.dograh.md). Les ajouts propres à Vocalia
(canal WhatsApp et couche SaaS) suivent la même licence.
