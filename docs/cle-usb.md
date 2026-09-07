# Écrire l'image sur une clé USB depuis Windows

## Obtenir l'image sans machine Linux

La construction demande `debootstrap`, `xorriso` et douze gigaoctets
libres, donc une machine Debian ou Ubuntu. Depuis Windows, inutile
d'installer WSL pour cela : le dépôt construit l'image sur GitHub.

Onglet **Actions** → *Image d'installation* → **Run workflow**. Vingt
minutes plus tard, l'ISO est en pièce jointe du run, avec sa somme de
contrôle. Le résumé du run affiche le SHA-256 à comparer après
téléchargement.

Une étiquette `v…` poussée sur le dépôt publie en plus une *release*, dont
le lien de téléchargement est direct et permanent.

## D'abord, ce qui bloque tout le monde

**Le Secure Boot doit être désactivé.** L'image embarque un GRUB construit
localement, qui n'est signé par personne. Un micrologiciel avec Secure Boot
actif — c'est le réglage d'usine de presque toutes les machines depuis
2012 — refusera de l'amorcer, souvent sans message clair : la clé est
simplement ignorée et la machine démarre sur son disque interne.

Au redémarrage, entrer dans le firmware (`F2`, `Suppr`, `F10` ou `Échap`
selon le constructeur), chercher *Secure Boot* dans l'onglet Boot ou
Security, le passer sur *Disabled*, enregistrer.

C'est une limite réelle de l'image, pas un oubli : signer une chaîne
d'amorçage suppose une clé reconnue par Microsoft, ou l'enrôlement d'une
clé personnelle dans le firmware de chaque machine.

**`E:` n'est pas la bonne cible.** Une lettre Windows désigne une
*partition*. Une image amorçable doit être écrite sur le *disque entier* —
table de partitions comprise, puisque c'est elle qui rend la clé
démarrable. Écrire sur `E:` produit une clé qui contient les fichiers mais
ne démarre pas.

Les outils ci-dessous demandent le disque, pas la lettre. Il faut donc
identifier lequel est la clé, et ne pas se tromper : **l'écriture est
irréversible et efface tout.**

## Identifier la clé sans risque

Dans PowerShell, en administrateur :

```powershell
Get-Disk | Format-Table Number, FriendlyName, @{n='Go';e={[int]($_.Size/1GB)}}, BusType
```

La clé se reconnaît à `BusType = USB` et à sa taille. Retenir son
**Number** — c'est lui qui compte, pas la lettre.

Vérification supplémentaire, si un doute subsiste : débrancher la clé,
relancer la commande, rebrancher, relancer. Le disque qui apparaît et
disparaît est le bon.

## Écrire — Rufus

[Rufus](https://rufus.ie) est le chemin le plus court.

1. Sélectionner le périphérique — vérifier la taille et le nom.
2. *Sélection* → l'ISO `agent-os-*.iso`.
3. **Au moment de valider, Rufus propose « mode Image ISO » ou « mode
   Image DD ». Choisir DD.** L'image est hybride : son secteur d'amorçage
   et sa partition EFI intégrée doivent être copiés octet pour octet. Le
   mode ISO recopie les fichiers et casse l'amorçage UEFI.
4. Schéma de partition : *GPT*, cible *UEFI (non CSM)* pour une machine
   récente ; *MBR* / *BIOS* pour une machine ancienne. En mode DD, Rufus
   suit l'image et le choix importe peu.

## Écrire — sans Rufus

Depuis WSL, si tu l'as :

```bash
# Identifier le disque : la clé apparaît sous /dev/sdX
lsblk -o NAME,SIZE,MODEL,TRAN
sudo dd if=/mnt/c/Users/toi/Downloads/agent-os.iso of=/dev/sdX \
        bs=4M status=progress conv=fsync
```

Depuis PowerShell en administrateur, sans rien installer :

```powershell
# 2 est le Number relevé plus haut — le vérifier deux fois.
$disque = 2
$iso = "C:\Users\toi\Downloads\agent-os.iso"

Clear-Disk -Number $disque -RemoveData -Confirm:$false
$flux = [System.IO.File]::OpenRead($iso)
$cible = New-Object System.IO.FileStream "\\.\PhysicalDrive$disque",
         'Open','Write','None'
$flux.CopyTo($cible, 4MB)
$cible.Flush(); $cible.Close(); $flux.Close()
```

Retirer la clé proprement (*Éjecter*) : sans cela, Windows peut n'avoir
pas encore vidé son cache d'écriture, et la clé sera incomplète.

## Démarrer dessus

Redémarrer et ouvrir le menu d'amorçage — `F12` chez Dell et Lenovo,
`F9` chez HP, `Échap` puis `F9` chez d'autres. Choisir l'entrée USB.

Si la clé n'apparaît pas :

- Secure Boot est encore actif (voir plus haut) ;
- ou le *Fast Boot* saute l'énumération USB : le désactiver ;
- ou la machine est en mode *CSM/Legacy* seul alors que la clé a été
  écrite en mode ISO : la réécrire en mode DD.

Le menu GRUB propose quatre entrées. La seconde, *mode sûr*, désactive
l'accélération graphique et l'ACPI — à essayer si l'écran reste noir après
le choix.

## Installer, ou seulement essayer

*Session live* démarre sans rien écrire sur les disques. C'est le bon
premier essai : il vérifie que le matériel est reconnu — réseau, disque,
clavier — sans engager quoi que ce soit.

```bash
ip addr          # la carte réseau est-elle vue ?
lsblk            # les disques sont-ils vus ?
free -h          # la mémoire annoncée est-elle là ?
```

*Installer agent-os* lance `agentos-installer`, qui **efface entièrement le
disque choisi**. Il refuse le disque portant le système en cours
d'exécution, donc il ne peut pas s'effacer lui-même.

```bash
sudo agentos-installer --simulation   # afficher le plan sans rien écrire
sudo agentos-installer
```

## Avant d'installer sur le disque interne

**`agentos-installer` efface intégralement le disque choisi.** Si ce disque
contient des données, ou le système Windows de la machine, tout disparaît.
Il n'y a pas de partitionnement partiel, pas de cohabitation avec un autre
système : l'installateur prend le disque entier.

Lancer d'abord `sudo agentos-installer --simulation`. Il affiche le plan
calculé pour la taille réelle du disque, sans rien écrire. Vérifier que le
disque nommé est bien celui qu'on croit — `lsblk` donne la taille et le
modèle de chacun.

L'installateur refuse le disque qui porte le système en cours d'exécution,
donc la clé d'installation ne peut pas s'effacer elle-même. Il ne peut en
revanche pas deviner que le disque visé contient tes photos.

Une clé de 2 Go suffit pour l'image : elle ne sert qu'à démarrer et à
essayer. C'est l'installation sur le disque interne qui demande de la
place.

## Installer sur une seconde clé USB

Possible aussi, comme banc d'essai prolongé : rien n'est touché sur le
disque interne. Deux réserves.

L'installateur **exige 120 Gio au minimum** — le partitionnement réserve
40 Gio au système et le reste à la mémoire et aux modèles. Une clé de
32 ou 64 Go sera refusée. Il faut une clé de 128 Go, ou un SSD externe.

Et la mémoire de l'agent est une base SQLite écrite en continu. Une clé
USB ordinaire, dont la mémoire flash n'a ni cache ni bonne répartition de
l'usure, s'usera vite et sera lente. Pour un essai de quelques jours, sans
importance. Pour un usage durable, un SSD externe.

## Sans matériel du tout

QEMU vérifie l'amorçage sans clé ni redémarrage :

```bash
qemu-system-x86_64 -m 4096 -smp 2 -cdrom agent-os.iso
```

C'est ce qui trouve les défauts d'image — GRUB, initrd, démarrage des
services. Ce que cela ne trouve pas : les pilotes de *ton* matériel. Les
deux essais sont complémentaires.
