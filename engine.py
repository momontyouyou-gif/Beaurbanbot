"""Чистая игровая логика UNDERGROUND (без привязки к Telegram)."""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from locations import LOCATIONS

CHOP = "chop"
DOLL = "doll"
VISITOR = "visitor"

ROLE_TITLES = {
    CHOP: "ЧОП",
    DOLL: "Кукла",
    VISITOR: "Посетитель",
}

MIN_PLAYERS = 4
MAX_PLAYERS = 20

DOLLS_BY_COUNT = {
    4: 1, 5: 2, 6: 2, 7: 2, 8: 3, 9: 3, 10: 4,
    11: 4, 12: 4, 13: 4, 14: 5, 15: 5, 16: 5,
    17: 6, 18: 6, 19: 7, 20: 7,
}


class Phase(Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    MORNING = "morning"
    FINISHED = "finished"


@dataclass
class Player:
    id: int
    name: str
    role: str | None = None
    alive: bool = True


@dataclass
class NightResult:
    stolen: str | None
    detained: list[Player]
    winner: str | None  # "chop" | "doll" | None


@dataclass
class Game:
    chat_id: int
    creator_id: int
    rng: random.Random = field(default_factory=random.Random)
    players: dict[int, Player] = field(default_factory=dict)
    phase: Phase = Phase.LOBBY
    location: dict | None = None
    items_left: list[str] = field(default_factory=list)
    chop_actions: dict[int, tuple[str, int]] = field(default_factory=dict)
    doll_votes: dict[int, str] = field(default_factory=dict)
    night_no: int = 0

    def add_player(self, uid: int, name: str) -> bool:
        if self.phase != Phase.LOBBY or uid in self.players or len(self.players) >= MAX_PLAYERS:
            return False
        self.players[uid] = Player(uid, name)
        return True

    def remove_player(self, uid: int) -> bool:
        if self.phase != Phase.LOBBY:
            return False
        return self.players.pop(uid, None) is not None

    def start(self) -> None:
        if self.phase != Phase.LOBBY:
            raise ValueError("Игра уже идёт")
        n = len(self.players)
        if n < MIN_PLAYERS:
            raise ValueError(f"Нужно минимум {MIN_PLAYERS} игрока")
        self.location = self.rng.choice(LOCATIONS)
        self.items_left = list(self.location["items"])
        dolls = DOLLS_BY_COUNT[n]
        roles = [CHOP] * 2 + [DOLL] * dolls + [VISITOR] * (n - 2 - dolls)
        self.rng.shuffle(roles)
        for p, r in zip(self.players.values(), roles):
            p.role = r
            p.alive = True

    def alive(self, role: str | None = None) -> list[Player]:
        return [p for p in self.players.values() if p.alive and (role is None or p.role == role)]

    def total_items(self) -> int:
        return len(self.location["items"]) if self.location else 0

    def begin_night(self) -> None:
        self.night_no += 1
        self.phase = Phase.NIGHT
        self.chop_actions.clear()
        self.doll_votes.clear()

    def chop_act(self, uid: int, action: str, target: int) -> str:
        p = self.players.get(uid)
        if self.phase != Phase.NIGHT:
            raise ValueError("Сейчас не ночь")
        if not p or not p.alive or p.role != CHOP:
            raise ValueError("Вы не можете это сделать")
        if uid in self.chop_actions:
            raise ValueError("Вы уже сделали выбор этой ночью")
        t = self.players.get(target)
        if action not in ("check", "detain") or not t or not t.alive or target == uid:
            raise ValueError("Неверная цель")
        self.chop_actions[uid] = (action, target)
        return t.role if action == "check" else ""

    def doll_vote(self, uid: int, item: str) -> None:
        p = self.players.get(uid)
        if self.phase != Phase.NIGHT:
            raise ValueError("Сейчас не ночь")
        if not p or not p.alive or p.role != DOLL:
            raise ValueError("Вы не можете это сделать")
        if item not in self.items_left:
            raise ValueError("Этого предмета уже нет")
        self.doll_votes[uid] = item

    def night_complete(self) -> bool:
        return (
            all(p.id in self.chop_actions for p in self.alive(CHOP))
            and all(p.id in self.doll_votes for p in self.alive(DOLL))
        )

    def resolve_night(self) -> NightResult:
        stolen = None
        if self.doll_votes:
            tally = Counter(self.doll_votes.values())
            top = max(tally.values())
            stolen = self.rng.choice(sorted(i for i, c in tally.items() if c == top))
            self.items_left.remove(stolen)

        detained_ids = {t for a, t in self.chop_actions.values() if a == "detain"}
        detained = [self.players[i] for i in sorted(detained_ids)]
        for p in detained:
            p.alive = False

        self.phase = Phase.MORNING
        return NightResult(stolen, detained, self.winner())

    def winner(self) -> str | None:
        if not self.alive(DOLL):
            return CHOP
        if not self.items_left or not self.alive(CHOP):
            return DOLL
        return None

    def finish(self) -> None:
        self.phase = Phase.FINISHED
