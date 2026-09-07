# agent-os

Une distribution Linux dont le userland est un agent autonome. Pas de
bureau, pas de navigateur : un seul service au démarrage, qui se souvient,
décide et exécute.

> Le dépôt s'appelle `ia-os`, le logiciel `agent-os` : c'est ce dernier nom
> que portent le paquet Python, le service systemd et la commande
> `agentosctl`.

## Ce que c'est, et ce que ce n'est pas

**Ce n'est pas un noyau écrit de zéro.** agent-os s'appuie sur le noyau
Linux et sur Debian stable. Écrire un noyau demanderait d'écrire aussi les
pilotes du disque, de la carte réseau et du contrôleur USB de chaque
machine cible — des années-homme pour un résultat qui, sans eux, n'aurait
ni mémoire persistante ni accès distant. Ce qui est sur mesure ici, c'est
tout ce qui tourne au-dessus du noyau.

**Ce n'est pas un modèle qui s'héberge lui-même.** Les poids de Claude
appartiennent à Anthropic et ne sont pas exportables. Ce qui tourne sur
votre machine, c'est le runtime : la boucle de décision, les outils, la
mémoire, l'ordonnanceur. Son cerveau est soit l'API Claude, soit un modèle
open-weights local, soit les deux avec bascule automatique.

**Ce que c'est :** une image installable qui transforme une machine en
agent qui travaille seul — avec une mémoire qui survit aux redémarrages,
une automatisation qui n'a besoin de personne, et une réplication vers
Postgres quand plusieurs machines doivent partager ce qu'elles savent.

## Installation

Deux chemins, selon que la machine te sert aussi de poste de travail.

### Sur une machine Omarchy — elle reste ton poste

```bash
git clone https://github.com/morpheus45/ia-os && cd ia-os/omarchy
./install.sh
```

Le service démarre au boot, avant toute session graphique. Le bureau gagne
un indicateur dans la barre, trois raccourcis et des notifications. Le
modèle local descend à 7 Gio pour cohabiter avec Hyprland et un
navigateur — voir [omarchy/README.md](omarchy/README.md).

Fonctionne sur n'importe quel Arch sous Hyprland ; l'intégration de la
barre suppose la disposition de waybar d'Omarchy.

### Sur une machine dédiée — sans écran, sans bureau

```bash
sudo ./build/build-iso.sh                    # produit une ISO amorçable
sudo dd if=build/out/agent-os-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

Démarrer sur la clé, puis `sudo agentos-installer`. Le modèle local dispose
alors de 11 Gio au lieu de 7, faute de bureau avec qui partager.

Sur une Debian ou une Arch déjà installée, l'ISO n'est pas nécessaire —
l'installateur détecte la distribution :

```bash
sudo ./system/install-system.sh
```

Voir [docs/installation.md](docs/installation.md) pour le détail.

## La machine visée

Le projet est dimensionné pour 16 Gio de mémoire vive et un disque de
500 Go. C'est ce qui explique plusieurs choix qui paraîtraient arbitraires
autrement.

Ces valeurs concernent la machine dédiée ; sur un poste Omarchy, c'est
l'installateur d'Omarchy qui partitionne, et agent-os s'installe dans le
système existant.

| Partition | Taille sur 500 Go | Rôle |
|---|---|---|
| EFI | 512 Mio | amorçage |
| swap | 16 Gio | hibernation |
| `/` | 40 Gio | système et runtime |
| `/var/lib/agentos` | ~82 Gio | mémoire, index, journaux |
| `/var/lib/models` | ~327 Gio | modèles locaux |

Un disque vendu « 500 Go » offre 465 Gio : l'installateur calcule le plan à
partir de la taille réelle, jamais d'un tableau figé.

## Comment ça marche

```
    console web ─┐
    agentosctl ──┼─→ API locale (127.0.0.1:8787)
    SSH ─────────┘         │
                           ▼
                    ┌─────────────┐
     cron, files ──▶│ ordonnanceur│──▶ boucle de l'agent ──▶ outils
                    └─────────────┘          │              (shell, fichiers,
                           │                 ▼               réseau, mémoire,
                           │          routeur de cerveaux    automatisation)
                           │             ╱          ╲
                           │      API Claude    modèle local
                           ▼
                    ┌──────────────────────────────┐
                    │ mémoire : SQLite + vecteurs  │──▶ Supabase (chiffré)
                    └──────────────────────────────┘
```

**Mémoire.** Un seul fichier SQLite porte le journal épisodique, les faits
sémantiques, les vecteurs et la file de synchro — ce qui rend instantané et
restauration atomiques sur une machine qui peut être coupée sans préavis.
La recherche croise BM25 et similarité vectorielle par fusion réciproque
des rangs. Les vecteurs sont quantifiés en int8, ce qui divise par quatre
leur empreinte : décisif quand un modèle local occupe déjà 11 Gio des 16.

**Cerveau.** Un routeur présente l'API Claude et le serveur local comme un
seul modèle et bascule de l'un à l'autre sur panne. Son disjoncteur borne
la latence : sans lui, chaque tâche paierait le délai d'expiration distant
complet avant de se rabattre, ce qui rendrait la machine inutilisable
pendant une coupure réseau plutôt que simplement plus lente.

**Automatisation.** Des plannings en notation cron déposent des travaux
dans une file persistée. Les travaux sont réessayés avec un délai
croissant, et ceux qu'une coupure a interrompus reviennent en file au
démarrage suivant.

**Mémoire distante.** La synchro pousse ce qui est nouveau et tire ce qui a
changé. Les conflits sont tranchés par horloge de Lamport, puis par
identifiant de nœud à égalité : deux machines restées hors ligne convergent
vers le même état quel que soit l'ordre de reconnexion.

## La mémoire est du contexte, jamais une consigne

C'est la règle qui structure tout le reste. Une mémoire persistante est
utile, et c'est aussi un moyen de persistance pour un attaquant : une
charge utile n'a pas besoin d'aboutir d'un coup, elle peut déposer des
fragments et les assembler des semaines plus tard, quand plus personne ne
relit ce que l'agent a mémorisé.

Chaque souvenir porte donc un niveau de confiance :

| Niveau | Origine | Traitement |
|---|---|---|
| `operator` | console ou CLI — vous | présenté comme du contexte |
| `agent` | observation de l'agent | encadré, annoncé comme donnée |
| `external` | fichier, page web, autre machine | encadré, annoncé comme donnée |

Ce qui n'est pas `operator` arrive au modèle dans une enveloppe qui dit
explicitement de ne pas exécuter ce qu'elle contient. Aucun outil ne peut
attribuer `operator` — sinon il suffirait à un contenu hostile de demander
à l'agent d'enregistrer quelque chose « comme venant de l'opérateur ». Ce
qui arrive d'une autre machine est plafonné à `external`, faute de quoi
compromettre un seul nœud suffirait à injecter des consignes réputées
fiables dans toute la flotte.

Deux filtres s'appliquent à l'écriture, seul moment où l'on peut encore
agir : les formes de secrets connues sont caviardées, et les caractères
Unicode invisibles retirés — un relecteur humain ne les voit pas, le modèle
si.

## Usage

```bash
agentosctl doctor                    # diagnostic, fonctionne démon éteint
agentosctl status                    # état du runtime
agentosctl ask "vérifie l'espace disque et note ce que tu trouves"
agentosctl memory search "sauvegarde"
agentosctl schedule add nuit --cron "0 3 * * *" --job agent
agentosctl sync                      # forcer une synchro distante
```

La console web est sur `http://127.0.0.1:8787`. Elle n'écoute que la boucle
locale ; pour y accéder à distance, un tunnel suffit :

```bash
ssh -L 8787:127.0.0.1:8787 admin@machine
```

## Configuration

`/etc/agentos/config.toml` pour les réglages, `/etc/agentos/secrets.env`
(0640, `root:agentos`) pour les secrets. Les secrets ne vont jamais dans le
TOML : `systemctl show` exposerait l'unité entière.

```bash
ANTHROPIC_API_KEY=sk-ant-...      # sans elle, seul le modèle local sert
AGENTOS_API_TOKEN=...             # tiré au hasard à l'installation
AGENTOS_REMOTE_URL=https://xxx.supabase.co
AGENTOS_SUPABASE_KEY=...          # « service_role », jamais « anon »
AGENTOS_REMOTE_KEY=...            # phrase chiffrant la mémoire avant envoi
```

## Modèle local

Déposer un fichier GGUF dans `/var/lib/models`, puis
`systemctl start agentos-model`. Sur 16 Gio sans GPU, la cible réaliste est
un modèle de 7 à 14 milliards de paramètres quantifié en 4 bits : quelques
jetons par seconde. Assez pour de l'automatisation de fond, pas pour du
dialogue interactif.

Le service de modèle est plafonné à 11 Gio et le runtime à 3 Gio. Sans ces
bornes, le noyau tuerait l'orchestrateur pour laisser vivre le modèle.

## Mémoire partagée entre machines

```bash
psql "$DSN" -f db/supabase/001_schema.sql
psql "$DSN" -f db/supabase/002_rls.sql
```

RLS est actif sans aucune politique pour `anon` : sans cela, la clé publiée
dans les clients lirait la mémoire de toutes les machines.

## Développement

```bash
cd runtime
python3 -m unittest discover -s tests -t .
```

Le runtime ne dépend que de la bibliothèque standard. `numpy` et
`cryptography` sont utilisés s'ils sont présents : le premier fait passer
la recherche vectorielle d'un balayage Python à un produit matriciel, le
second chiffre la mémoire avant envoi. En leur absence, l'index est
plafonné et la synchro chiffrée refuse de démarrer — plutôt que de partir
en clair.

## Limites connues

- Un modèle local sur processeur est lent. C'est un repli, pas un équivalent.
- Le confinement des outils réduit la surface d'attaque ; ce n'est pas une
  frontière de sécurité entre processus du même utilisateur. L'isolation
  réelle vient du compte dédié et des restrictions systemd.
- L'index vectoriel en mémoire est plafonné. Au-delà, les souvenirs anciens
  restent atteignables par la recherche plein texte, pas par la voie
  sémantique.
- Le repli lexical par hachage rapproche des formes voisines, pas des
  synonymes : « mémoire vive » ne trouve pas « ram_go ». Un vrai modèle
  d'embedding est nécessaire pour cela.
- Les instants cron sont calculés en heure locale. Au changement d'heure,
  une tâche placée dans l'heure sautée ne se déclenche pas ce jour-là.

## Documentation

- [omarchy/README.md](omarchy/README.md) — machine Omarchy, agent intégré au bureau
- [installation.md](docs/installation.md) — de l'ISO à la première tâche
- [architecture.md](docs/architecture.md) — les choix et leurs raisons
- [memoire.md](docs/memoire.md) — modèle de mémoire et recherche
- [securite.md](docs/securite.md) — surface d'attaque et défenses

## Licence

MIT.
