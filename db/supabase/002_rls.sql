-- Verrouillage des accès.
--
-- La règle par défaut de PostgreSQL est permissive : sans RLS, la clé
-- « anon » d'un projet Supabase — celle qui est publiée dans les clients —
-- lit toute la mémoire de toutes les machines. On active donc RLS partout,
-- et on n'accorde explicitement l'accès qu'au rôle `service_role`, qui est
-- le seul dont la clé reste sur les machines.
--
-- Appliquer après 001_schema.sql.

alter table agentos_nodes    enable row level security;
alter table agentos_episodes enable row level security;
alter table agentos_facts    enable row level security;
alter table agentos_vectors  enable row level security;
alter table agentos_sync_log enable row level security;

-- Aucune politique pour anon ni authenticated : sans politique, RLS refuse
-- tout. `service_role` contourne RLS par construction, ce qui suffit aux
-- machines. Écrire des politiques pour anon serait le seul moyen d'ouvrir
-- la mémoire au navigateur — ne pas le faire est délibéré.

-- Révocation explicite : ceinture et bretelles, au cas où une migration
-- ultérieure ajouterait une politique par inadvertance.
revoke all on agentos_nodes,    agentos_episodes, agentos_facts,
              agentos_vectors,  agentos_sync_log from anon, authenticated;

-- Purge côté serveur : la rétention doit pouvoir être tenue même si la
-- machine qui a écrit ne se rallume jamais.
create or replace function agentos_purge(retention_days integer default 400)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    removed integer;
begin
    delete from agentos_episodes
     where ts < now() - make_interval(days => retention_days);
    get diagnostics removed = row_count;

    -- Les faits ne sont pas purgés par l'âge : c'est précisément leur rôle
    -- de survivre au journal dont ils sont issus.
    delete from agentos_vectors v
     where not exists (select 1 from agentos_episodes e where e.uid = v.uid)
       and not exists (select 1 from agentos_facts    f where f.uid = v.uid);

    delete from agentos_sync_log where at < now() - interval '90 days';
    return removed;
end;
$$;

revoke all on function agentos_purge(integer) from public, anon, authenticated;
