"""Hygiène de la mémoire : secrets et caractères invisibles.

Une mémoire persistante est utile, et c'est aussi un vecteur de
persistance pour un attaquant. Une charge utile n'a pas besoin d'aboutir
en un seul coup : elle peut déposer des fragments et les assembler des
semaines plus tard, quand plus personne ne relit ce que l'agent a
mémorisé. Deux défenses se posent donc à l'écriture, là où le contenu
entre — pas à la lecture, où il serait déjà trop tard.

La première retire les caractères de contrôle invisibles : espaces de
largeur nulle, surcharges bidirectionnelles, jointures. Un humain qui
relit la mémoire ne les voit pas ; le modèle, lui, les lit.

La seconde caviarde les formes de secrets connues. C'est un filet de
sécurité, pas un classificateur : elle reconnaît les préfixes documentés
et les clés privées, elle ne prétend pas détecter tout secret possible.
Elle vaut surtout parce que la mémoire part vers un serveur distant —
sans elle, une clé lue une fois dans un fichier finirait répliquée hors
de la machine.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Caractères invisibles ou capables d'inverser le sens de lecture.
INVISIBLE = (
    "\u200b\u200c\u200d\u2060\ufeff"      # largeurs nulles, jointures, BOM
    "\u202a\u202b\u202c\u202d\u202e"      # surcharges bidirectionnelles
    "\u2066\u2067\u2068\u2069"            # isolats bidirectionnels
    "\u00ad"                              # trait d'union conditionnel
)
_INVISIBLE_RE = re.compile(f"[{INVISIBLE}]")

#: Formes de secrets reconnaissables sans ambiguïté, par leur préfixe ou
#: leur structure. Le libellé sert de remplacement, pour que la mémoire
#: garde la trace de ce qui a été retiré.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("clé Anthropic",   re.compile(r"sk-ant-[A-Za-z0-9_\-]{24,}")),
    ("clé OpenAI",      re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{32,}")),
    ("jeton GitHub",    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("clé AWS",         re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("clé Google",      re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jeton Slack",     re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("clé privée",      re.compile(
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
        re.DOTALL)),
    ("jeton JWT",       re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("URL avec mot de passe", re.compile(
        r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]{3,}@[^\s/]+")),
    # Affectation explicite d'un secret : le nom de la variable fait foi,
    # la valeur doit être assez longue pour ne pas être un mot ordinaire.
    ("secret affecté",  re.compile(
        r"(?i)\b(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|auth[_\-]?token"
        r"|password|passwd|client[_\-]?secret)\b\s*[:=]\s*"
        r"[\"']?([A-Za-z0-9/+=_\-]{16,})[\"']?")),
]


@dataclass(slots=True)
class Report:
    """Ce qui a été modifié à l'entrée en mémoire."""

    text: str
    invisible_removed: int = 0
    secrets: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.secrets is None:
            self.secrets = []

    @property
    def clean(self) -> bool:
        return not self.secrets and self.invisible_removed == 0

    def summary(self) -> str:
        parts = []
        if self.invisible_removed:
            parts.append(f"{self.invisible_removed} caractère(s) invisible(s) retiré(s)")
        if self.secrets:
            parts.append("caviardé : " + ", ".join(sorted(set(self.secrets))))
        return " ; ".join(parts)


def strip_invisible(text: str) -> tuple[str, int]:
    """Retire les caractères invisibles et de direction."""
    cleaned, count = _INVISIBLE_RE.subn("", text)
    # Les catégories Cf (format) restantes sont retirées aussi, à l'exception
    # du saut de ligne et de la tabulation qui portent une vraie mise en forme.
    out = []
    for char in cleaned:
        if unicodedata.category(char) == "Cf":
            count += 1
            continue
        out.append(char)
    return "".join(out), count


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Remplace les secrets reconnus par un marqueur nommant leur nature.

    On caviarde plutôt que de refuser l'écriture : perdre l'observation
    entière parce qu'elle contient une clé priverait l'agent d'un souvenir
    souvent utile, alors que le marqueur en conserve le sens.
    """
    found: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            found.append(label)
            return f"[{label} caviardée]"

        text = pattern.sub(replace, text)
    return text, found


def sanitize(text: str) -> Report:
    """Passe un contenu au filtre avant de le mémoriser."""
    cleaned, removed = strip_invisible(text)
    cleaned, secrets = redact_secrets(cleaned)
    return Report(text=cleaned, invisible_removed=removed, secrets=secrets)


def looks_sensitive(text: str) -> bool:
    """Vrai si le texte contient une forme de secret reconnue."""
    return any(pattern.search(text) for _, pattern in SECRET_PATTERNS)
