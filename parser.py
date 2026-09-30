"""Разбор сообщений вида «жим 80x8x3».

Поддерживаемые форматы (в одной строке одно упражнение, строк может быть несколько):
    жим лежа 80x8x3          -> 3 подхода по 8 повторов с 80 кг
    жим 80х8                 -> 1 подход (русская «х» тоже работает)
    жим 82,5x8x3             -> дробный вес через запятую или точку
    жим 80x8,8,7             -> повторы по подходам: 8, 8, 7
    жим 80x8x2 85x6          -> несколько групп подходов в одной строке
    подтягивания x10x3       -> без веса (свой вес), можно и 0x10x3 или бв10x3
    подтягивания +10x8x3     -> с доп. весом 10 кг
Разделители: x, х, ×, *
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SEP = r"[xх×*]"
GROUP_RE = re.compile(
    rf"^(?:(?P<bw>бв|bw){SEP}?|(?P<w>\+?\d+(?:[.,]\d+)?)?{SEP})"  # вес, «бв» или пусто
    rf"(?P<reps>\d+(?:[,/]\d+)*)"                                 # повторы: 8 или 8,8,7
    rf"(?:{SEP}(?P<sets>\d+))?$",                                  # число подходов
    re.IGNORECASE,
)

MAX_SETS = 20
MAX_REPS = 200
MAX_WEIGHT = 1000


@dataclass
class ParsedLine:
    exercise: str
    sets: list[tuple[float, int]] = field(default_factory=list)  # (вес, повторы)


class ParseError(ValueError):
    pass


def normalize_name(name: str) -> str:
    name = name.lower().replace("ё", "е")
    name = re.sub(r"[^\w\s-]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _parse_group(token: str) -> list[tuple[float, int]] | None:
    m = GROUP_RE.match(token)
    if not m:
        return None
    w_raw = m.group("w") or ""
    if not w_raw:
        weight = 0.0
    else:
        weight = float(w_raw.lstrip("+").replace(",", "."))
    reps_list = [int(r) for r in re.split(r"[,/]", m.group("reps"))]
    n_sets = int(m.group("sets")) if m.group("sets") else None

    if len(reps_list) > 1 and n_sets:
        raise ParseError(f"«{token}»: либо повторы через запятую, либо число подходов, не оба сразу")
    if n_sets:
        reps_list = reps_list * n_sets

    if not reps_list or len(reps_list) > MAX_SETS:
        raise ParseError(f"«{token}»: странное число подходов")
    if any(r <= 0 or r > MAX_REPS for r in reps_list):
        raise ParseError(f"«{token}»: странное число повторов")
    if weight < 0 or weight > MAX_WEIGHT:
        raise ParseError(f"«{token}»: странный вес")
    return [(weight, r) for r in reps_list]


def parse_line(line: str) -> ParsedLine | None:
    """Возвращает None, если в строке нет подходов (значит, это не запись тренировки)."""
    tokens = line.strip().split()
    if not tokens:
        return None

    # Первый токен, похожий на подходы, отделяет название от цифр.
    split_at = None
    for i, tok in enumerate(tokens):
        if _parse_group(tok.rstrip(".;")) is not None:
            split_at = i
            break
    if split_at is None:
        return None
    if split_at == 0:
        raise ParseError(f"«{line.strip()}»: не вижу названия упражнения")

    name = normalize_name(" ".join(tokens[:split_at]))
    if not name:
        raise ParseError(f"«{line.strip()}»: не вижу названия упражнения")

    sets: list[tuple[float, int]] = []
    for tok in tokens[split_at:]:
        tok = tok.rstrip(".;")
        group = _parse_group(tok)
        if group is None:
            raise ParseError(f"«{tok}» не понял. Пример: жим 80x8x3")
        sets.extend(group)
    if len(sets) > MAX_SETS:
        raise ParseError("слишком много подходов в одной строке")
    return ParsedLine(name, sets)


def parse_message(text: str) -> list[ParsedLine]:
    result = []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed:
            result.append(parsed)
    return result


def e1rm(weight: float, reps: int) -> float:
    """Расчётный разовый максимум по формуле Эпли."""
    if weight <= 0:
        return 0.0
    return weight if reps == 1 else weight * (1 + reps / 30)


def fmt_w(w: float) -> str:
    return f"{w:g}"


def fmt_sets(sets: list[tuple[float, int]]) -> str:
    """[(80,8)]*3 -> '80×8×3';  [(80,8),(80,8),(80,7),(85,6)] -> '80×8,8,7, 85×6'"""
    groups: list[tuple[float, list[int]]] = []
    for w, r in sets:
        if groups and groups[-1][0] == w:
            groups[-1][1].append(r)
        else:
            groups.append((w, [r]))
    parts = []
    for w, reps in groups:
        head = fmt_w(w) if w > 0 else "бв"
        if len(set(reps)) == 1:
            parts.append(f"{head}×{reps[0]}" + (f"×{len(reps)}" if len(reps) > 1 else ""))
        else:
            parts.append(f"{head}×" + ",".join(map(str, reps)))
    return ", ".join(parts)
