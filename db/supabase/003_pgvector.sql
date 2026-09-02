-- Recherche sémantique côté serveur (facultatif).
--
-- À n'appliquer que si toutes les machines de la flotte tournent le même
-- modèle d'embedding : une colonne `vector` impose une dimension unique, et
-- mélanger les modèles produirait un classement dénué de sens plutôt qu'une
-- erreur visible.
--
-- Le client, lui, cherche en local et n'a pas besoin de ceci. L'intérêt est
-- ailleurs : interroger la mémoire de toute la flotte depuis un tableau de
-- bord, sans réveiller les machines.

create extension if not exists vector;

alter table agentos_vectors
    add column if not exists embedding vector(768);

-- IVFFlat plutôt qu'un index exact : au-delà de quelques dizaines de
-- milliers de lignes, le parcours complet devient plus coûteux que la perte
-- de rappel d'un index approché. `lists` se règle autour de la racine du
-- nombre de lignes attendu.
create index if not exists agentos_vectors_embedding
    on agentos_vectors using ivfflat (embedding vector_cosine_ops)
    with (lists = 100);

create or replace function agentos_search(
    query_embedding vector(768),
    match_count integer default 10,
    min_similarity real default 0.25
)
returns table (uid text, node text, scope text, similarity real)
language sql
stable
as $$
    select v.uid,
           v.node,
           v.scope,
           (1 - (v.embedding <=> query_embedding))::real as similarity
      from agentos_vectors v
     where v.embedding is not null
       and (1 - (v.embedding <=> query_embedding)) >= min_similarity
     order by v.embedding <=> query_embedding
     limit match_count;
$$;
