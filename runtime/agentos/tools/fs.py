"""Outils de fichiers, confinés à un espace de travail.

Le confinement est vérifié après résolution des liens symboliques. Le
contrôler sur le chemin demandé ne servirait à rien : un lien déposé dans
l'espace de travail et pointant vers `/etc` suffirait à en sortir.
"""

from __future__ import annotations

import os
from pathlib import Path

from .registry import Registry, ToolError

#: Au-delà, on tronque : un fichier entier dans le contexte du modèle coûte
#: cher et n'apporte presque jamais plus que ses premiers milliers de
#: caractères.
MAX_READ = 100_000
MAX_WRITE = 5_000_000


class Workspace:
    """Racine unique sous laquelle tous les chemins doivent retomber."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).expanduser()
        try:
            # strict=False : le fichier peut ne pas exister encore (écriture).
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ToolError(f"chemin illisible : {relative} ({exc})") from exc

        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError(
                f"« {relative} » sort de l'espace de travail ({self.root}) — refusé"
            )
        return resolved


def register(registry: Registry, workspace: Workspace) -> None:
    def read_file(path: str, max_chars: int = MAX_READ) -> str:
        target = workspace.resolve(path)
        if not target.is_file():
            raise ToolError(f"« {path} » n'est pas un fichier")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"lecture impossible : {exc}") from exc
        if len(text) > max_chars:
            return text[:max_chars] + f"\n… (tronqué, {len(text)} caractères au total)"
        return text

    def write_file(path: str, content: str, append: bool = False) -> str:
        if len(content) > MAX_WRITE:
            raise ToolError(f"contenu trop volumineux ({len(content)} > {MAX_WRITE})")
        target = workspace.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "a" if append else "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise ToolError(f"écriture impossible : {exc}") from exc
        return f"{'ajouté à' if append else 'écrit dans'} {path} ({len(content)} caractères)"

    def list_files(path: str = ".", pattern: str = "*") -> list[str]:
        target = workspace.resolve(path)
        if not target.is_dir():
            raise ToolError(f"« {path} » n'est pas un répertoire")
        entries = []
        for entry in sorted(target.glob(pattern)):
            kind = "d" if entry.is_dir() else "f"
            size = entry.stat().st_size if entry.is_file() else 0
            entries.append(f"{kind} {size:>10}  {entry.relative_to(workspace.root)}")
        return entries or ["(vide)"]

    def delete_file(path: str) -> str:
        target = workspace.resolve(path)
        if target == workspace.root:
            raise ToolError("la racine de l'espace de travail ne peut pas être supprimée")
        if not target.exists():
            raise ToolError(f"« {path} » n'existe pas")
        if target.is_dir():
            try:
                target.rmdir()
            except OSError as exc:
                raise ToolError(f"répertoire non vide ou protégé : {exc}") from exc
            return f"répertoire {path} supprimé"
        target.unlink()
        return f"{path} supprimé"

    registry.register(
        "fs_read", "Lit un fichier texte de l'espace de travail.",
        {"type": "object",
         "properties": {
             "path": {"type": "string", "description": "chemin relatif à l'espace de travail"},
             "max_chars": {"type": "integer", "default": MAX_READ}},
         "required": ["path"]},
        read_file,
    )
    registry.register(
        "fs_write", "Écrit ou complète un fichier de l'espace de travail.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "content": {"type": "string"},
             "append": {"type": "boolean", "default": False}},
         "required": ["path", "content"]},
        write_file, sensitive=True,
    )
    registry.register(
        "fs_list", "Liste les fichiers d'un répertoire de l'espace de travail.",
        {"type": "object",
         "properties": {"path": {"type": "string", "default": "."},
                        "pattern": {"type": "string", "default": "*"}},
         "required": []},
        list_files,
    )
    registry.register(
        "fs_delete", "Supprime un fichier ou un répertoire vide.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        delete_file, sensitive=True,
    )
