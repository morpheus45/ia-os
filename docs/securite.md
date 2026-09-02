# Sécurité

## Ce qui est défendu, et ce qui ne l'est pas

Le modèle de menace tient en une phrase : **le contenu que l'agent lit peut
être hostile, et le modèle peut se tromper.** Tout le reste en découle.

Ce document dit aussi ce qui n'est pas couvert. Un dispositif de sécurité
dont on ignore les limites est plus dangereux qu'une absence de dispositif,
parce qu'il inspire une confiance qu'il ne mérite pas.

## Frontière

Les confinements décrits ici réduisent la surface d'attaque. **Ce ne sont
pas des frontières de sécurité entre processus du même utilisateur.** Tout
ce que le compte `agentos` peut faire reste atteignable par d'autres voies
pour qui exécute déjà du code sous ce compte.

L'isolation réelle vient du compte système dédié et des restrictions
systemd : `ProtectSystem=strict`, aucune capacité, appels système filtrés,
`/var/lib/agentos/workspace` monté sans droit d'exécution.

Pour se protéger d'un processus local malveillant, il faut des comptes
séparés, des conteneurs, ou une machine distincte.

## Injection par le contenu

C'est le risque principal. L'agent lit des fichiers, récupère des pages,
exécute des commandes dont il relit la sortie. Chacun de ces contenus peut
contenir des phrases impératives adressées au modèle.

Trois défenses :

1. Tout ce qui n'est pas `operator` arrive dans une enveloppe explicite,
   accompagnée de la consigne système de ne jamais exécuter ce qu'elle
   contient.
2. Ce que rend un outil est consigné comme `external`, jamais comme
   observation de l'agent.
3. Aucun outil ne peut attribuer `operator` — sinon il suffirait à un
   contenu hostile de demander à l'agent d'enregistrer quelque chose
   « comme venant de l'opérateur ».

Ces défenses réduisent le risque, elles ne l'annulent pas : un modèle peut
toujours se laisser convaincre. C'est pourquoi les outils sensibles
disparaissent en exécution non surveillée.

## Persistance par la mémoire

Une charge utile n'a pas besoin d'aboutir d'un coup. Elle peut déposer des
fragments et les assembler des semaines plus tard, quand plus personne ne
relit ce que l'agent a mémorisé.

D'où le plancher de similarité sur la recherche vectorielle : sans lui,
n'importe quel souvenir remonte sur n'importe quelle requête, ce qui offre
à un fragment planté une occasion à chaque tâche.

D'où aussi le plafond de confiance sur la synchro : sans lui, compromettre
une seule machine de la flotte suffirait à injecter des consignes réputées
fiables dans toutes les autres.

Pour un travail qui manipule des contenus étrangers toute la journée,
envisager une machine dédiée dont la mémoire est remise à zéro.

## Exécution de commandes

`shell=True` est absent. La commande est découpée par `shlex` puis passée à
`execve` : ni pipe, ni redirection, ni substitution, donc aucun moyen de
transformer un argument en commande. Le nom de l'exécutable est confronté à
une liste d'autorisation, et un chemin absolu est refusé — sinon un binaire
homonyme déposé ailleurs contournerait la liste.

L'environnement transmis est réduit à `PATH`, `LANG` et `HOME` : une
commande décidée par le modèle ne doit pas pouvoir lire la clé d'API du
service.

## Fichiers

Le confinement est vérifié **après** résolution des liens symboliques. Le
contrôler sur le chemin demandé ne servirait à rien : un lien déposé dans
l'espace de travail et pointant vers `/etc` suffirait à en sortir.

## Réseau

L'outil de récupération refuse les adresses privées, de bouclage,
lien-local et réservées. La résolution DNS est faite avant la requête :
contrôler la seule chaîne du nom laisserait passer un domaine public
pointant délibérément vers `127.0.0.1`.

Sans ce filtre, une consigne glissée dans une page suffirait à faire
interroger par l'agent la box, l'imprimante ou un service d'administration
du réseau local — depuis l'intérieur du pare-feu.

## API locale

Elle n'écoute que la boucle locale, et n'a ni TLS ni gestion de comptes. Ce
n'est pas un oubli : les ajouter donnerait l'illusion qu'elle peut être
exposée.

Les routes de lecture sont ouvertes à quiconque atteint la boucle locale ;
celles qui agissent exigent un jeton, comparé à temps constant — une
comparaison naïve laisse deviner le jeton octet par octet par la mesure du
temps.

Le jeton est tiré au hasard à l'installation. Une valeur par défaut
partagée par toutes les images serait pire que pas de jeton du tout.

## Console

Le contenu mémorisé peut venir d'une page web. La console ne l'insère donc
jamais autrement que par `textContent`, et sa CSP interdit tout chargement
externe. L'afficher en HTML reviendrait à exécuter chez l'opérateur ce
qu'un tiers a écrit ailleurs.

## Secrets

Dans `/etc/agentos/secrets.env`, en 0640 `root:agentos`, jamais dans le
TOML ni dans l'unité systemd — `systemctl show` expose l'unité entière.

La mémoire est caviardée avant écriture, ce qui compte d'autant plus
qu'elle part vers un serveur distant.

Le chiffrement distant s'appuie sur `cryptography`. Si la bibliothèque
manque alors que le chiffrement est demandé, la synchro refuse de démarrer
au lieu de se rabattre sur du clair : une machine qui envoie sa mémoire en
clair alors que l'opérateur a demandé le contraire est un problème pire
qu'une synchro à l'arrêt.

La sonde de disponibilité fait un aller-retour de chiffrement complet. Se
contenter d'importer le paquet racine réussirait sur une installation aux
liaisons natives incomplètes, puis échouerait au premier chiffrement.

## Côté serveur

RLS est actif sur toutes les tables, sans aucune politique pour `anon` ni
`authenticated`. Sans politique, RLS refuse tout ; `service_role` le
contourne par construction, ce qui suffit aux machines.

La règle par défaut de PostgreSQL est permissive : sans RLS, la clé `anon`
d'un projet Supabase — celle qui est publiée dans les clients — lirait la
mémoire de toutes les machines.

## Système

Pare-feu en refus par défaut, SSH seul port ouvert, avec limitation du
débit de connexions. La console et le serveur de modèle n'écoutent que la
boucle locale et ne sont donc jamais joignables de l'extérieur, même si un
réglage les exposait par mégarde.

SSH par clé uniquement : un mot de passe sur une machine qui tourne seule
finit toujours par être faible ou réutilisé.

Les clés d'hôte SSH et l'identifiant machine sont régénérés à
l'installation. Les laisser dans l'image donnerait la même identité à
toutes les machines, ce qui permet de se faire passer pour l'une d'elles.

## Signaler un problème

Ouvrir une issue sans détail exploitable, et demander un canal privé.
