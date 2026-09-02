# Installation

## Construire l'image

Sur une machine Debian ou Ubuntu, avec les outils de construction :

```bash
sudo apt install debootstrap xorriso squashfs-tools \
                 grub-efi-amd64-bin grub-pc-bin mtools
sudo ./build/build-iso.sh
```

Comptez vingt à quarante minutes et 12 Gio d'espace libre : le chroot pèse
environ 2,5 Gio avant compression. L'image sort dans `build/out/`, avec sa
somme de contrôle.

Variables utiles :

| Variable | Défaut | Usage |
|---|---|---|
| `SUITE` | `trixie` | version de Debian |
| `MIROIR` | `deb.debian.org` | miroir local pour accélérer |
| `REFAIRE` | — | `REFAIRE=1` repart d'un chroot neuf |
| `SORTIE` | `build/out` | répertoire de l'image |

Le script réutilise le chroot existant d'une construction à l'autre : une
seconde image après un changement du runtime prend quelques minutes.

## Essayer sans matériel

```bash
qemu-system-x86_64 -m 4096 -cdrom build/out/agent-os-*.iso
```

Suffisant pour vérifier l'amorçage et l'installateur. Pas pour un modèle
local, qui demande plus de mémoire que ce qu'on donne à une machine
virtuelle de test.

## Écrire sur une clé

```bash
lsblk                       # identifier la clé — cette commande efface tout
sudo dd if=build/out/agent-os-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

L'image est hybride : elle démarre en BIOS comme en UEFI.

## Installer sur le disque

Démarrer sur la clé. La session s'ouvre seule sur un compte `live`, sans
mot de passe — l'image sert à installer, pas à travailler.

```bash
sudo agentos-installer --simulation    # afficher le plan sans rien écrire
sudo agentos-installer
```

L'installateur refuse un disque qui porte le système en cours d'exécution
ou dont des partitions sont montées : sans ce contrôle, installer sur la
clé de démarrage détruirait le système en train de tourner, et l'erreur ne
se verrait qu'une fois le partitionnement fait.

Il demande ensuite un compte d'administration et une clé SSH publique.
**Sans clé, l'accès distant sera impossible** : le système refuse
l'authentification par mot de passe.

### Chiffrer la mémoire

`--chiffrer` place la partition `/var/lib/agentos` sous LUKS2. La mémoire
contient tout ce que la machine a vu ; sur une machine physiquement
exposée, c'est justifié.

En contrepartie, le démarrage exige une phrase de passe au clavier. Une
machine censée repartir seule après une coupure de courant ne repartira
pas. À réserver aux cas où une présence au redémarrage est acceptable.

## Premier démarrage

```bash
sudo nano /etc/agentos/secrets.env     # clés API, Supabase, chiffrement
sudo agentosctl doctor                 # vérifier avant de démarrer
sudo systemctl restart agentos
agentosctl status
```

`doctor` fonctionne démon éteint — c'est précisément le moment où on en a
besoin. Il distingue ce qui bloque de ce qui dégrade : une clé API absente
n'empêche pas la machine de fonctionner sur son modèle local.

## Mémoire distante

Une seule fois pour toute la flotte, depuis n'importe quelle machine :

```bash
psql "$DSN" -f /usr/share/doc/agent-os/db/supabase/001_schema.sql
psql "$DSN" -f /usr/share/doc/agent-os/db/supabase/002_rls.sql
```

Puis sur chaque machine, dans `secrets.env` : `AGENTOS_REMOTE_URL`,
`AGENTOS_SUPABASE_KEY` (la clé `service_role`) et `AGENTOS_REMOTE_KEY`. La
même phrase de chiffrement doit être posée sur toutes les machines qui
doivent se relire.

Activer ensuite `enabled = true` dans la section `[remote]` du TOML, et
redémarrer le service.

## Modèle local

```bash
sudo -u agentos curl -L -o /var/lib/models/modele.gguf "<url du GGUF>"
sudo systemctl start agentos-model
agentosctl doctor        # doit voir le serveur local
```

Le service ne démarre que si un `.gguf` est présent : sans cette condition,
une machine sans modèle verrait systemd relancer en boucle un serveur qui
ne peut pas démarrer.

Après un changement de modèle d'embedding, l'index doit être reconstruit —
les anciens vecteurs ne sont plus comparables aux nouveaux :

```bash
sudo systemctl stop agentos
sudo -u agentos agentosctl memory reindex
sudo systemctl start agentos
```

## Accès à la console

```bash
ssh -L 8787:127.0.0.1:8787 admin@machine
```

Puis `http://127.0.0.1:8787`. Le jeton est dans `secrets.env`.

Ne pas changer `api.host` pour exposer la console : elle n'a ni TLS ni
gestion de comptes. Pour un accès permanent, un reverse proxy qui termine
le TLS et authentifie.

## Mise à jour du runtime

```bash
git pull
sudo ./system/install-system.sh      # idempotent
sudo systemctl restart agentos
```

La mémoire et la configuration ne sont pas touchées. Les migrations de
schéma s'appliquent au démarrage suivant, dans une transaction : une
migration interrompue ne laisse pas un schéma à moitié créé.
