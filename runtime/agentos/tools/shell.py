"""Exécution de commandes, sans interpréteur.

`shell=True` est volontairement absent : la commande est découpée par
`shlex` puis passée telle quelle à `execve`. Il n'y a donc ni pipe, ni
redirection, ni substitution — et donc aucun moyen de transformer un
argument en commande. Le nom de l'exécutable est ensuite confronté à une
liste d'autorisation ; ce qui n'y figure pas ne s'exécute pas.

Ce n'est pas une frontière de sécurité entre processus : l'agent tourne
sous un compte système, et tout ce que ce compte peut faire reste
atteignable par d'autres voies. C'est une réduction de surface, qui rend
difficile la transformation d'une réponse de modèle en commande
arbitraire. L'isolation réelle vient du service systemd et du compte
dédié, décrits dans `system/`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .registry import Registry, ToolError

#: Au-delà, la sortie est tronquée : un `find /` complet n'apporte rien au
#: modèle et sature son contexte.
MAX_OUTPUT = 20_000


def register(
    registry: Registry,
    *,
    allowlist: list[str],
    workdir: str | Path,
    timeout_s: int = 60,
) -> None:
    permitted = set(allowlist)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    def run(command: str) -> str:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ToolError(f"commande mal formée : {exc}") from exc
        if not parts:
            raise ToolError("commande vide")

        program = os.path.basename(parts[0])
        if program not in permitted:
            raise ToolError(
                f"« {program} » n'est pas dans la liste d'autorisation. "
                f"Autorisés : {', '.join(sorted(permitted))}"
            )
        if program != parts[0] and "/" in parts[0]:
            # Un chemin absolu contournerait la liste si un binaire homonyme
            # était déposé ailleurs ; on n'accepte que la résolution par PATH.
            raise ToolError("indiquer la commande par son nom, sans chemin")

        try:
            completed = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=workdir,
                check=False,
                # Environnement réduit : ni clés d'API ni DSN ne doivent
                # transiter par une commande décidée par le modèle.
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                     "HOME": str(workdir)},
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"« {command} » n'a pas rendu la main en {timeout_s}s") from None
        except (OSError, ValueError) as exc:
            raise ToolError(f"exécution impossible : {exc}") from exc

        output = completed.stdout
        if completed.stderr:
            output += ("\n--- erreur ---\n" if output else "") + completed.stderr
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n… (tronqué, {len(output)} caractères)"
        if completed.returncode != 0:
            output = f"(code de retour {completed.returncode})\n{output}"
        return output or "(aucune sortie)"

    registry.register(
        "shell",
        "Exécute une commande système parmi une liste restreinte. "
        "Pas de pipe, de redirection ni de substitution : la commande est "
        f"exécutée directement. Autorisées : {', '.join(sorted(permitted))}.",
        {"type": "object",
         "properties": {"command": {
             "type": "string",
             "description": "commande et arguments, par exemple « df -h /var »"}},
         "required": ["command"]},
        run,
    )
