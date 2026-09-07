"""Configuration d'agent-os.

La configuration vient de trois sources, par priorité croissante :
le défaut codé ici, `/etc/agentos/config.toml`, puis l'environnement.
Les secrets ne sont jamais lus depuis le TOML : ils viennent de
l'environnement ou de `/etc/agentos/secrets.env`, dont les droits sont
0600 et le propriétaire `agentos`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# Racine déplaçable : la production utilise `/`, les tests un répertoire
# temporaire. Tout chemin absolu du système est dérivé d'ici.
ROOT = Path(os.environ.get("AGENTOS_ROOT", "/"))

ETC = ROOT / "etc/agentos"
STATE = ROOT / "var/lib/agentos"
MODELS = ROOT / "var/lib/models"
LOGS = ROOT / "var/log/agentos"
RUN = ROOT / "run/agentos"


@dataclass
class MemoryConfig:
    #: Base SQLite unique : épisodique, sémantique, vecteurs et file de synchro.
    database: str = str(STATE / "memory.db")
    #: Dimension des vecteurs. Doit correspondre au modèle d'embedding choisi ;
    #: un changement invalide l'index existant (voir `agentosctl memory reindex`).
    vector_dim: int = 768
    #: Au-delà, une entrée épisodique est résumée puis compactée.
    episodic_retention_days: int = 400
    #: Nombre de candidats tirés de chaque index avant fusion des rangs.
    search_candidates: int = 64
    #: Constante de la fusion réciproque des rangs (RRF).
    rrf_k: int = 60
    #: Similarité minimale pour qu'un voisin vectoriel compte. Sans plancher,
    #: la recherche vectorielle renvoie toujours ses plus proches voisins,
    #: aussi éloignés soient-ils : le contexte se remplirait de souvenirs
    #: hors sujet à chaque question, et n'importe quel souvenir planté
    #: finirait par remonter sur n'importe quelle requête. Calibré pour un
    #: vrai modèle d'embedding ; le repli lexical, qui score plus bas,
    #: contribue naturellement moins.
    min_similarity: float = 0.25


@dataclass
class RemoteConfig:
    #: Active la synchro. Sans URL, le runtime reste purement local.
    enabled: bool = False
    #: URL du projet Supabase, par exemple https://abcdefgh.supabase.co
    url: str = ""
    #: DSN PostgreSQL direct, pour un Postgres auto-hébergé plutôt que Supabase.
    dsn: str = ""
    #: Identifiant stable de cette machine dans la mémoire partagée.
    node_id: str = ""
    #: Période de synchro en secondes.
    interval_s: int = 300
    #: Lignes poussées par lot.
    batch: int = 500
    #: Chiffrement du contenu avant envoi. La clé vient de AGENTOS_REMOTE_KEY.
    encrypt: bool = True
    #: Niveau de confiance maximal attribuable à ce qui arrive d'un autre
    #: nœud. La machine ne peut pas vérifier qu'une affirmation marquée
    #: « operator » ailleurs vient bien de l'humain : sans plafond, il
    #: suffirait de compromettre une seule machine de la flotte pour
    #: injecter des consignes réputées fiables dans toutes les autres.
    trust_ceiling: str = "external"


@dataclass
class BrainConfig:
    #: Ordre de préférence. Le routeur prend le premier backend sain.
    order: list[str] = field(default_factory=lambda: ["anthropic", "local"])
    #: Modèle distant.
    anthropic_model: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 4096
    #: Serveur local compatible OpenAI : llama.cpp `--server` ou Ollama.
    local_url: str = "http://127.0.0.1:8080/v1"
    local_model: str = "qwen2.5-7b-instruct-q4_k_m"
    #: Modèle d'embedding servi par le même serveur local.
    embed_model: str = "nomic-embed-text-v1.5"
    #: Délai avant de considérer un backend muet.
    timeout_s: int = 120
    #: Après cet échec consécutif, le backend est écarté pour `cooldown_s`.
    failure_threshold: int = 3
    cooldown_s: int = 60


@dataclass
class SchedulerConfig:
    #: Jobs exécutés en parallèle. Au-delà de 4 sur 16 Go, la RAM devient
    #: le facteur limitant dès qu'un modèle local est chargé.
    concurrency: int = 2
    #: Pas de l'horloge. Un job ne peut pas démarrer plus finement que ça.
    tick_s: float = 1.0
    #: Reprises et backoff exponentiel plafonné.
    max_attempts: int = 5
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 900.0
    #: Un job dépassant cette durée est tué.
    job_timeout_s: int = 1800


@dataclass
class ApiConfig:
    #: Écoute en loopback. L'exposition réseau passe par un reverse proxy
    #: avec TLS et authentification, jamais par ce serveur directement.
    host: str = "127.0.0.1"
    port: int = 8787
    #: Jeton exigé sur les routes mutantes. Vide = lecture seule pour tous.
    token: str = ""


@dataclass
class AgentConfig:
    #: Nom affiché dans la console et dans la mémoire partagée.
    name: str = "agent-os"
    #: Tours de boucle maximum sur une seule tâche, garde-fou anti-emballement.
    max_turns: int = 12
    #: Racines auxquelles les outils fichier sont confinés.
    workspace: str = str(STATE / "workspace")
    #: Commandes shell autorisées, par nom d'exécutable.
    shell_allowlist: list[str] = field(
        default_factory=lambda: [
            "cat", "cut", "date", "df", "du", "echo", "find", "grep", "head",
            "hostname", "ls", "ps", "sed", "sort", "stat", "tail", "uname",
            "uniq", "uptime", "wc",
        ]
    )
    shell_timeout_s: int = 60


@dataclass
class Config:
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    #: Anomalies rencontrées au chargement, quand le mode indulgent est
    #: demandé. `doctor` doit pouvoir les rapporter au lieu d'abandonner :
    #: c'est justement lorsque la configuration est illisible qu'on a besoin
    #: d'un diagnostic.
    problemes: list[str] = field(default_factory=list)

    # --- secrets, jamais persistés dans le TOML ---
    anthropic_key: str = ""
    remote_key: str = ""
    #: Clé de service Supabase. Jamais la clé « anon » : celle-ci est publiée
    #: dans les clients et RLS lui refuse tout accès, par construction.
    remote_api_key: str = ""

    @property
    def state_dir(self) -> Path:
        return Path(self.memory.database).parent


def _coerce(value: Any, target: type) -> Any:
    """Convertit une valeur TOML/env vers le type déclaré du champ."""
    if target is bool and isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if target in (int, float, str) and not isinstance(value, target):
        return target(value)
    if target is list and isinstance(value, str):
        return [p for p in (x.strip() for x in value.split(",")) if p]
    return value


def _apply(section: Any, values: dict[str, Any]) -> None:
    """Écrase les champs d'une dataclass depuis un dict, en ignorant l'inconnu.

    Une clé absente de la dataclass est silencieusement laissée de côté :
    une faute de frappe dans le TOML ne doit pas empêcher le boot.
    """
    known = {f.name: f for f in fields(section)}
    for key, value in values.items():
        spec = known.get(key)
        if spec is None:
            continue
        setattr(section, key, _coerce(value, spec.type if isinstance(spec.type, type) else type(getattr(section, key))))


#: Surcharges par variable d'environnement : ENV -> (section, champ).
_ENV_MAP: dict[str, tuple[str, str]] = {
    "AGENTOS_DB": ("memory", "database"),
    "AGENTOS_REMOTE_DSN": ("remote", "dsn"),
    "AGENTOS_REMOTE_URL": ("remote", "url"),
    "AGENTOS_REMOTE_ENABLED": ("remote", "enabled"),
    "AGENTOS_NODE_ID": ("remote", "node_id"),
    "AGENTOS_LOCAL_URL": ("brain", "local_url"),
    "AGENTOS_LOCAL_MODEL": ("brain", "local_model"),
    "AGENTOS_MODEL": ("brain", "anthropic_model"),
    "AGENTOS_API_HOST": ("api", "host"),
    "AGENTOS_API_PORT": ("api", "port"),
    "AGENTOS_API_TOKEN": ("api", "token"),
}


def _load_secrets_env(path: Path) -> dict[str, str]:
    """Lit un fichier `KEY=value` sans l'exporter dans l'environnement global."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load(path: Path | None = None, *, strict: bool = True) -> Config:
    """Assemble la configuration effective.

    En mode strict — celui du démon — une configuration illisible ou
    malformée arrête le démarrage : mieux vaut ne pas partir que partir avec
    des réglages qui ne sont pas ceux qu'on croit. En mode indulgent, les
    anomalies sont consignées dans `problemes` et les défauts prennent le
    relais, ce dont `doctor` a besoin pour diagnostiquer au lieu de mourir.
    """
    cfg = Config()

    toml_path = path or ETC / "config.toml"
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (tomllib.TOMLDecodeError, PermissionError, UnicodeDecodeError, OSError) as exc:
        message = f"{toml_path} illisible : {exc}"
        if strict:
            raise SystemExit(f"config illisible : {message}") from exc
        cfg.problemes.append(message)
        raw = {}

    for name in (f.name for f in fields(cfg)):
        section = getattr(cfg, name)
        if is_dataclass(section) and isinstance(raw.get(name), dict):
            _apply(section, raw[name])

    secrets_path = ETC / "secrets.env"
    secrets = _load_secrets_env(secrets_path)
    if not secrets:
        # `exists()` lève quand le répertoire parent n'est pas traversable :
        # sonder les droits ne doit pas être plus fragile que de les ignorer.
        try:
            present, lisible = secrets_path.exists(), os.access(secrets_path, os.R_OK)
        except OSError:
            present, lisible = True, False
        if present and not lisible:
            cfg.problemes.append(
                f"{secrets_path} illisible : ajouter l'utilisateur au groupe "
                "agentos, ou passer par sudo")
    env = {**secrets, **os.environ}

    for var, (section_name, field_name) in _ENV_MAP.items():
        if var in env and env[var] != "":
            section = getattr(cfg, section_name)
            current = getattr(section, field_name)
            setattr(section, field_name, _coerce(env[var], type(current)))

    if "AGENTOS_BRAIN_ORDER" in env:
        cfg.brain.order = [p.strip() for p in env["AGENTOS_BRAIN_ORDER"].split(",") if p.strip()]

    cfg.anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    cfg.remote_key = env.get("AGENTOS_REMOTE_KEY", "")
    cfg.remote_api_key = env.get("AGENTOS_SUPABASE_KEY", "")

    # Une machine sans identité ne peut pas être départagée lors de la
    # réconciliation distante : on retombe sur le hostname.
    if not cfg.remote.node_id:
        cfg.remote.node_id = os.uname().nodename

    return cfg
