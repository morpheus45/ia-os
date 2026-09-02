"""Analyse d'expressions cron et calcul du prochain déclenchement.

Le calcul procède par sauts et non minute par minute : depuis l'instant
courant, on avance au prochain mois autorisé, puis au prochain jour, puis
à l'heure, puis à la minute. Une expression rare — « le 29 février à
3h07 » — se résout ainsi en quelques dizaines d'itérations là où un
balayage naïf en demanderait deux millions.

Les instants sont calculés en heure locale, parce que c'est ce qu'un
utilisateur veut dire par « tous les jours à 3 h ». Conséquence à
connaître : au passage à l'heure d'hiver une tâche planifiée dans l'heure
répétée se déclenche une fois, et au passage à l'heure d'été une tâche
planifiée dans l'heure sautée est ignorée ce jour-là. Pour une périodicité
qui doit être insensible au fuseau, régler la machine en UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable

#: minute, heure, jour du mois, mois, jour de la semaine
FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "heure", "jour du mois", "mois", "jour de la semaine")

_MONTHS = {name: index for index, name in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_DAYS = {name: index for index, name in enumerate(
    "sun mon tue wed thu fri sat".split())}

#: Raccourcis usuels, développés avant analyse.
SHORTCUTS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_STEP = re.compile(r"^(?P<range>[^/]+)(?:/(?P<step>\d+))?$")


class CronError(ValueError):
    """Expression cron invalide."""


def _parse_field(spec: str, index: int) -> set[int]:
    low, high = FIELD_RANGES[index]
    values: set[int] = set()

    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"champ « {FIELD_NAMES[index]} » : élément vide")

        match = _STEP.match(part)
        if match is None:
            raise CronError(f"champ « {FIELD_NAMES[index]} » : « {part} » incompréhensible")
        body, step_text = match.group("range"), match.group("step")

        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"champ « {FIELD_NAMES[index]} » : pas nul dans « {part} »")

        if body == "*":
            start, end = low, high
        elif "-" in body[1:]:
            start_text, _, end_text = body.partition("-")
            start, end = _named(start_text, index), _named(end_text, index)
        else:
            start = end = _named(body, index)
            if step_text:  # « 5/15 » signifie 5, 20, 35… jusqu'au haut du champ
                end = high

        if not (low <= start <= high and low <= end <= high):
            raise CronError(
                f"champ « {FIELD_NAMES[index]} » : « {part} » hors de [{low}, {high}]")
        if start > end:
            raise CronError(f"champ « {FIELD_NAMES[index]} » : intervalle inversé « {part} »")

        values.update(range(start, end + 1, step))

    # Cron accepte 7 pour dimanche ; on le normalise sur 0.
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return values


def _named(token: str, index: int) -> int:
    token = token.strip().lower()
    if index == 3 and token in _MONTHS:
        return _MONTHS[token]
    if index == 4:
        if token in _DAYS:
            return _DAYS[token]
        if token == "7":
            return 0
    try:
        return int(token)
    except ValueError as exc:
        raise CronError(f"champ « {FIELD_NAMES[index]} » : « {token} » n'est pas un nombre") from exc


class Cron:
    """Expression cron à cinq champs, résolue en instants successifs."""

    __slots__ = ("expression", "minutes", "hours", "days", "months", "weekdays",
                 "_dom_restricted", "_dow_restricted")

    def __init__(self, expression: str) -> None:
        raw = expression.strip()
        if raw.lower() in SHORTCUTS:
            raw = SHORTCUTS[raw.lower()]
        fields = raw.split()
        if len(fields) != 5:
            raise CronError(
                f"expression « {expression} » : {len(fields)} champs au lieu de 5 "
                "(minute heure jour mois jour-semaine)")

        self.expression = expression.strip()
        self.minutes = _parse_field(fields[0], 0)
        self.hours = _parse_field(fields[1], 1)
        self.days = _parse_field(fields[2], 2)
        self.months = _parse_field(fields[3], 3)
        self.weekdays = _parse_field(fields[4], 4)
        # Quand jour-du-mois et jour-de-semaine sont tous deux restreints, cron
        # déclenche si l'un OU l'autre correspond — et non les deux. Il faut
        # donc mémoriser lesquels étaient explicites.
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    def __repr__(self) -> str:
        return f"Cron({self.expression!r})"

    # -- correspondance ---------------------------------------------------

    def _day_matches(self, moment: datetime) -> bool:
        # `weekday()` compte le lundi comme 0 ; cron compte le dimanche.
        dow = (moment.weekday() + 1) % 7
        dom_ok = moment.day in self.days
        dow_ok = dow in self.weekdays
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and self._day_matches(moment)
        )

    # -- progression ------------------------------------------------------

    def next_after(self, moment: datetime, *, horizon_days: int = 1500) -> datetime | None:
        """Premier instant strictement postérieur à `moment` qui correspond.

        Renvoie None si rien ne correspond dans l'horizon — cas d'une
        expression impossible comme « 30 février ».
        """
        candidate = (moment + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = moment + timedelta(days=horizon_days)

        while candidate <= limit:
            if candidate.month not in self.months:
                candidate = _start_of_next_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = _start_of_next_day(candidate)
                continue
            if candidate.hour not in self.hours:
                nxt = _next_in(self.hours, candidate.hour)
                if nxt is None:
                    candidate = _start_of_next_day(candidate)
                else:
                    candidate = candidate.replace(hour=nxt, minute=0)
                continue
            if candidate.minute not in self.minutes:
                nxt = _next_in(self.minutes, candidate.minute)
                if nxt is None:
                    candidate = (candidate.replace(minute=0) + timedelta(hours=1))
                else:
                    candidate = candidate.replace(minute=nxt)
                continue
            return candidate
        return None

    def next_timestamp(self, after: float | None = None) -> float | None:
        """Variante travaillant en secondes epoch, ce qu'attend la base."""
        import time as _time

        base = datetime.fromtimestamp(after if after is not None else _time.time())
        moment = self.next_after(base)
        return moment.timestamp() if moment else None


def _start_of_next_day(moment: datetime) -> datetime:
    return (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_next_month(moment: datetime) -> datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return moment.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_in(allowed: Iterable[int], current: int) -> int | None:
    later = [value for value in allowed if value > current]
    return min(later) if later else None


def validate(expression: str) -> str:
    """Vérifie une expression et la renvoie normalisée, ou lève CronError."""
    return Cron(expression).expression
