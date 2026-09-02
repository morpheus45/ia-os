# Architecture

Ce document explique les choix, pas l'API. Chaque section part du problème
concret qui a imposé la solution.

## Une seule base

Journal, faits, vecteurs, file de travaux et file de synchro tiennent dans
un seul fichier SQLite. L'alternative — une base par domaine — aurait rendu
l'instantané non atomique : sauvegarder cinq fichiers pendant que la
machine écrit produit cinq états qui ne correspondent à aucun instant réel.

SQLite tourne en WAL, avec `synchronous=NORMAL`. Sur coupure brutale on
peut perdre la dernière transaction, jamais l'intégrité de la base ; le
gain en écritures est d'un ordre de grandeur sur disque mécanique.

Une connexion par thread, et un verrou de processus sérialise les
écritures : cela transforme les `SQLITE_BUSY` en attente ordonnée plutôt
qu'en erreur remontée à l'appelant.

## Identifiants triables

Les identifiants sont des ULID : 48 bits d'horodatage puis 80 bits d'aléa,
en base32. Deux machines qui écrivent hors ligne doivent produire des
identifiants qui ne collisionnent pas *et* qui restent ordonnés par date
une fois les mémoires fusionnées. Un entier auto-incrémenté ne peut faire
ni l'un ni l'autre ; un UUID4 fait le premier, pas le second.

Deux appels dans la même milliseconde incrémentent la partie aléatoire au
lieu de la retirer, ce qui garde l'ordre lexicographique aligné sur l'ordre
d'écriture. Une horloge recalée en arrière par NTP ne fait pas régresser la
suite.

## Recherche hybride

Les deux voies échouent sur des choses différentes. Le plein texte rate la
reformulation ; les vecteurs ratent le terme exact et rare — un
identifiant, un code d'erreur — que BM25 trouve immédiatement.

La fusion se fait par rangs et non par scores. Un score BM25 et un cosinus
ne vivent pas sur la même échelle, et leurs plages varient d'une requête à
l'autre : les combiner exigerait une calibration qui ne tiendrait pas. Les
rangs, eux, sont comparables sans rien calibrer.

Un plancher de similarité écarte les voisins trop éloignés. Sans lui, la
recherche vectorielle rend toujours ses plus proches voisins quelle que
soit la question — ce n'est pas une recherche mais un tirage, et un
souvenir planté finirait par remonter sur n'importe quelle requête.

## Quantification des vecteurs

En float32, 768 dimensions coûtent 3 Kio par souvenir : 200 000 souvenirs
occupent 600 Mio qu'on ne peut pas se permettre sur une machine dont un
modèle local réclame déjà 11 Gio. Quantifiés en int8 après normalisation,
ils tiennent dans 150 Mio.

L'erreur mesurée sur le cosinus est de 4·10⁻⁵, très en dessous du bruit du
modèle d'embedding lui-même. Les vecteurs étant normalisés avant
quantification, le produit scalaire *est* le cosinus : aucune division au
moment de la requête.

L'index est borné. Au-delà de sa capacité, les entrées les plus anciennes
sortent de la RAM mais restent en base et restent atteignables par la
recherche plein texte. Seule la voie sémantique se restreint aux souvenirs
récents — le bon arbitrage sur une machine contrainte.

## Routeur de cerveaux

Le routeur essaie les backends dans l'ordre configuré. Un disjoncteur
écarte celui qui échoue de façon répétée.

Son rôle principal est de borner la latence. Sans lui, chaque tâche paierait
le délai d'expiration du backend distant avant de se rabattre sur le local :
pendant une coupure réseau, la machine ne serait pas plus lente, elle serait
inutilisable. Les backends limitent aussi leurs propres tentatives à deux —
s'acharner sur un service mort avant de basculer annule l'intérêt d'avoir un
second cerveau.

Quand tous les disjoncteurs sont ouverts, le routeur tente quand même : une
tentative vouée à l'échec vaut mieux qu'un refus certain, et c'est elle qui
refermera le disjoncteur au retour du service.

## Plannings et travaux

Deux mécanismes distincts. Les *plannings* décrivent une récurrence en
notation cron ; à chaque tour d'horloge, ceux qui sont dus déposent un
*travail* dans la file. Les ouvriers ne connaissent que la file.

Cette séparation permet à un travail d'être réessayé sans dérégler la
récurrence, et à une tâche ponctuelle d'emprunter exactement le même chemin
d'exécution qu'une tâche périodique.

La prise d'un travail est atomique — sélection et passage à « running »
dans une seule instruction — de sorte que deux ouvriers ne peuvent pas
repartir avec le même. Un travail resté « running » après un arrêt brutal
est remis en file au démarrage suivant : personne ne le terminera tout seul.

Le délai de reprise croît exponentiellement et porte un bruit aléatoire.
Sans ce bruit, des travaux tombés ensemble sur une panne commune
retenteraient exactement au même instant, et retomberaient en panne pour la
même raison.

## Résolution des instants cron

Le calcul procède par sauts : depuis l'instant courant, on avance au
prochain mois autorisé, puis au jour, puis à l'heure, puis à la minute. Une
expression rare — « le 29 février à 3 h 07 » — se résout en quelques
dizaines d'itérations là où un balayage minute par minute en demanderait
deux millions.

Quand jour-du-mois et jour-de-semaine sont tous deux restreints, cron
déclenche si l'un **ou** l'autre correspond. Cette sémantique historique
surprend, mais s'en écarter surprendrait davantage.

## Deux régimes d'exécution

Un travail lancé depuis la console est surveillé : l'opérateur voit passer
les actions et peut interrompre. Un travail déclenché par un planning à 3 h
du matin ne l'est pas.

Le second reçoit une panoplie d'outils réduite — pas d'écriture de fichier,
pas de création de récurrence. Une récurrence créée par un modèle sans
témoin s'exécuterait indéfiniment sans que personne ne la relise.

## Bornes de la boucle

Trois, dont aucune n'est facultative : nombre de tours, échéance, liste
d'outils. Un modèle qui part dans la mauvaise direction ne s'arrête pas de
lui-même — il consomme des jetons ou du processeur jusqu'à ce qu'on
l'arrête.

Chaque appel d'outil est consigné en mémoire *avant* son résultat. C'est la
seule trace exploitable quand la boucle a été interrompue en cours de
route.

## Ordre d'arrêt

L'ordre d'arrêt est l'inverse exact de l'ordre de construction :
l'ordonnanceur se tait avant que la base ne se ferme. Autrement, un ouvrier
encore vivant écrirait dans une connexion fermée, et l'erreur
n'apparaîtrait que dans les journaux, après coup.
