"""Assemblage de la panoplie d'outils."""

from __future__ import annotations

from .fs import Workspace
from .registry import Outcome, Registry, Tool, ToolError

__all__ = ["Outcome", "Registry", "Tool", "ToolError", "Workspace", "build"]


def build(config, memory, scheduler=None, *, allow_sensitive: bool = True) -> Registry:
    """Construit le registre offert au modèle.

    `allow_sensitive` distingue les deux régimes d'exécution. En conduite
    manuelle, l'opérateur voit passer les actions et peut interrompre :
    écriture de fichiers et création de récurrences sont ouvertes. En
    exécution autonome — un travail déclenché par l'ordonnanceur à 3 h du
    matin — ces mêmes outils sont retirés, parce qu'une récurrence créée
    par un modèle sans témoin s'exécutera indéfiniment sans relecture.
    """
    from . import automation, fs, memory_tools, shell, web

    registry = Registry(allow_sensitive=allow_sensitive)
    workspace = Workspace(config.agent.workspace)

    fs.register(registry, workspace)
    shell.register(
        registry,
        allowlist=config.agent.shell_allowlist,
        workdir=workspace.root,
        timeout_s=config.agent.shell_timeout_s,
    )
    web.register(registry)
    memory_tools.register(registry, memory)
    if scheduler is not None:
        automation.register(registry, scheduler)
    return registry
