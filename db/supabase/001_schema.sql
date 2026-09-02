-- Schéma de la mémoire partagée entre machines.
--
-- Les tables reprennent la forme de la base locale, plus deux colonnes qui
-- n'ont de sens qu'entre plusieurs nœuds : `node` dit quelle machine a
-- écrit, `lamport` permet de départager deux écritures concurrentes. Une
-- horloge murale ne le peut pas de façon fiable entre machines dont les
-- horloges dérivent ou que NTP recale en arrière.
--
-- Appliquer avec :  psql "$DSN" -f 001_schema.sql

create extension if not exists pgcrypto;

-- Registre des machines. Sert à répondre à « qui a écrit ça, et quand
-- cette machine s'est-elle manifestée pour la dernière fois ».
create table if not exists agentos_nodes (
    node        text primary key,
    label       text        not null default '',
    first_seen  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    version     text        not null default ''
);

create table if not exists agentos_episodes (
    uid       text primary key,
    node      text        not null references agentos_nodes(node) on delete cascade,
    ts        timestamptz not null,
    kind      text        not null,
    actor     text        not null,
    session   text,
    content   text        not null,
    meta      jsonb       not null default '{}'::jsonb,
    lamport   bigint      not null default 0,
    -- Voir la note sur la confiance plus bas : une valeur reçue d'un autre
    -- nœud ne peut jamais valoir « operator » à l'arrivée.
    trust     text        not null default 'agent'
                check (trust in ('operator', 'agent', 'external')),
    encrypted boolean     not null default false,
    synced_at timestamptz not null default now()
);

create index if not exists agentos_episodes_ts      on agentos_episodes (ts desc);
create index if not exists agentos_episodes_lamport on agentos_episodes (lamport);
create index if not exists agentos_episodes_node    on agentos_episodes (node, lamport);
create index if not exists agentos_episodes_session on agentos_episodes (session, ts desc);

create table if not exists agentos_facts (
    uid        text primary key,
    node       text        not null references agentos_nodes(node) on delete cascade,
    subject    text        not null,
    predicate  text        not null,
    object     text        not null,
    confidence real        not null default 0.5,
    source_uid text,
    created    timestamptz not null,
    updated    timestamptz not null,
    revoked    boolean     not null default false,
    lamport    bigint      not null default 0,
    trust      text        not null default 'agent'
                 check (trust in ('operator', 'agent', 'external')),
    encrypted  boolean     not null default false,
    synced_at  timestamptz not null default now()
);

create index if not exists agentos_facts_lamport on agentos_facts (lamport);
create index if not exists agentos_facts_subject on agentos_facts (subject) where not revoked;

-- Un même triplet peut être affirmé par plusieurs machines : la contrainte
-- porte donc sur (nœud, triplet) et non sur le triplet seul. C'est la
-- réconciliation côté client qui décide lequel fait foi, en fonction de
-- l'horloge de Lamport et du niveau de confiance.
create unique index if not exists agentos_facts_triplet
    on agentos_facts (node, subject, predicate, object);

-- Vecteurs. La colonne reste en bytea plutôt qu'en `vector` de pgvector :
-- les nœuds peuvent tourner des modèles d'embedding différents, et une
-- colonne typée imposerait une dimension unique à toute la flotte. Voir
-- 003_pgvector.sql pour la variante indexée, à n'appliquer que si toutes
-- les machines partagent le même modèle.
create table if not exists agentos_vectors (
    uid     text primary key,
    node    text        not null references agentos_nodes(node) on delete cascade,
    scope   text        not null,
    dim     integer     not null,
    scale   real        not null,
    data    bytea       not null,
    updated timestamptz not null default now()
);

-- Journal des synchros, pour diagnostiquer une machine qui décroche.
create table if not exists agentos_sync_log (
    id        bigserial primary key,
    node      text        not null,
    direction text        not null check (direction in ('push', 'pull')),
    scope     text        not null,
    rows      integer     not null default 0,
    at        timestamptz not null default now(),
    error     text        not null default ''
);

create index if not exists agentos_sync_log_node on agentos_sync_log (node, at desc);
