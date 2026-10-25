"""Ma Sói – engine trò chơi.

Không phụ thuộc FastAPI để dễ test. Toàn bộ trạng thái của một phòng nằm trong
`Room`; mọi thay đổi chạy trên một event loop asyncio duy nhất nên không cần lock.
Mỗi khi trạng thái đổi, engine gọi `broadcast(room)` để lớp WebSocket gửi cho
từng người chơi một "góc nhìn" riêng (view_for) – không bao giờ gửi bí mật chung.
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

log = logging.getLogger("masoi")

MIN_PLAYERS = 4  # để test cho tiện; chơi thật nên đặt 10
MAX_PLAYERS = 30


class Role(str, Enum):
    WEREWOLF = "werewolf"
    VILLAGER = "villager"
    SEER = "seer"
    BODYGUARD = "bodyguard"
    WITCH = "witch"
    HUNTER = "hunter"


ROLE_VI = {
    Role.WEREWOLF: "Sói",
    Role.VILLAGER: "Dân làng",
    Role.SEER: "Tiên tri",
    Role.BODYGUARD: "Bảo vệ",
    Role.WITCH: "Phù thủy",
    Role.HUNTER: "Thợ săn",
}
SINGLE_ROLES = {Role.SEER, Role.BODYGUARD, Role.WITCH, Role.HUNTER}


class Phase(str, Enum):
    LOBBY = "lobby"
    NIGHT_GUARD = "night_guard"
    NIGHT_WOLVES = "night_wolves"
    NIGHT_SEER = "night_seer"
    NIGHT_WITCH = "night_witch"
    DAWN = "dawn"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    VERDICT = "verdict"
    HUNTER_SHOT = "hunter_shot"
    ENDED = "ended"


def suggest_roles(n: int) -> dict[str, int]:
    """Chia vai mặc định: ~1/4 là sói, mỗi vai đặc biệt 1 người, còn lại là dân."""
    roles = {
        Role.WEREWOLF: max(1, n // 4),
        Role.SEER: 1,
        Role.BODYGUARD: 1,
        Role.WITCH: 1,
        Role.HUNTER: 1 if n >= 8 else 0,
    }
    roles[Role.VILLAGER] = max(0, n - sum(roles.values()))
    return {r.value: c for r, c in roles.items()}


@dataclass
class Player:
    id: str
    name: str
    token: str
    is_host: bool = False
    role: Optional[Role] = None
    alive: bool = True
    connected: bool = False
    private_log: list[str] = field(default_factory=list)


@dataclass
class Settings:
    roles: dict[str, int] = field(default_factory=dict)
    night_step_seconds: int = 30
    discuss_seconds: int = 180
    vote_seconds: int = 60
    hunter_seconds: int = 20
    reveal_role_on_death: bool = True


class GameError(Exception):
    """Lỗi hợp lệ hoá – thông điệp được gửi thẳng cho người chơi."""


Broadcast = Callable[["Room"], Awaitable[None]]


class Room:
    def __init__(self, code: str, broadcast: Broadcast):
        self.code = code
        self._broadcast = broadcast
        self.players: dict[str, Player] = {}
        self.settings = Settings()
        self.roles_custom = False
        self.created_at = time.time()
        # thời lượng màn hình thông báo & thời gian chờ giả (test có thể chỉnh nhỏ)
        self.announce_seconds = 4.0
        self.fake_wait = (3.0, 6.0)
        self.task: asyncio.Task | None = None
        self._reset_game()

    # ------------------------------------------------------------ phòng chờ
    def _reset_game(self) -> None:
        self.phase = Phase.LOBBY
        self.day_no = 0
        self.deadline: float | None = None
        self.log: list[dict] = []
        self.winner: str | None = None
        self.actors: set[str] = set()
        self._step_done = asyncio.Event()
        self.last_guarded: str | None = None
        self.witch_heal_left = True
        self.witch_poison_left = True
        self.day_votes: dict[str, str] = {}
        self.hunter_shooter: str | None = None
        self.hunter_target: str | None = None
        self._reset_night()

    def _reset_night(self) -> None:
        self.guard_target: str | None = None
        self.wolf_votes: dict[str, str] = {}
        self.wolf_target: str | None = None
        self.seer_result: str | None = None
        self.witch_heal = False
        self.witch_poison: str | None = None

    def add_player(self, name: str, is_host: bool = False) -> Player:
        if self.phase != Phase.LOBBY:
            raise GameError("Ván đã bắt đầu, không thể vào thêm")
        if len(self.players) >= MAX_PLAYERS:
            raise GameError(f"Phòng đã đủ {MAX_PLAYERS} người")
        name = " ".join(name.split())[:20]
        if not name:
            raise GameError("Hãy nhập tên")
        if any(p.name.lower() == name.lower() for p in self.players.values()):
            raise GameError("Tên này đã có người dùng trong phòng")
        p = Player(id=secrets.token_hex(4), name=name,
                   token=secrets.token_urlsafe(16), is_host=is_host)
        self.players[p.id] = p
        self._auto_roles()
        return p

    def remove_player(self, pid: str) -> None:
        if self.phase != Phase.LOBBY:
            raise GameError("Không thể rời khi ván đang diễn ra")
        p = self.players.pop(pid, None)
        if p and p.is_host and self.players:
            next(iter(self.players.values())).is_host = True
        self._auto_roles()

    def _auto_roles(self) -> None:
        if not self.roles_custom:
            self.settings.roles = suggest_roles(len(self.players))

    def update_settings(self, data: dict) -> None:
        if self.phase != Phase.LOBBY:
            raise GameError("Chỉ đổi cấu hình được ở phòng chờ")
        try:
            if data.get("auto_roles"):
                self.roles_custom = False
                self._auto_roles()
            elif isinstance(data.get("roles"), dict):
                self.settings.roles = {
                    r.value: max(0, min(1 if r in SINGLE_ROLES else MAX_PLAYERS,
                                        int(data["roles"].get(r.value, 0))))
                    for r in Role
                }
                self.roles_custom = True
            limits = {"night_step_seconds": (10, 120), "discuss_seconds": (30, 900),
                      "vote_seconds": (15, 300), "hunter_seconds": (10, 60)}
            for key, (lo, hi) in limits.items():
                if key in data:
                    setattr(self.settings, key, max(lo, min(hi, int(data[key]))))
            if "reveal_role_on_death" in data:
                self.settings.reveal_role_on_death = bool(data["reveal_role_on_death"])
        except (TypeError, ValueError):
            raise GameError("Cấu hình không hợp lệ")

    def validate_roles(self) -> None:
        n, r = len(self.players), self.settings.roles
        if n < MIN_PLAYERS:
            raise GameError(f"Cần ít nhất {MIN_PLAYERS} người")
        total = sum(r.values())
        if total != n:
            raise GameError(f"Tổng số vai ({total}) phải bằng số người chơi ({n})")
        wolves = r.get(Role.WEREWOLF.value, 0)
        if wolves < 1:
            raise GameError("Cần ít nhất 1 Sói")
        if wolves >= n - wolves:
            raise GameError("Sói quá đông – phải ít hơn phe Dân")

    async def start(self) -> None:
        self.validate_roles()
        pool = [Role(k) for k, c in self.settings.roles.items() for _ in range(c)]
        random.shuffle(pool)
        self._reset_game()
        for p, role in zip(self.players.values(), pool):
            p.role, p.alive, p.private_log = role, True, []
        wolves = [p for p in self.players.values() if p.role == Role.WEREWOLF]
        for p in self.players.values():
            p.private_log.append(f"Vai của bạn: {ROLE_VI[p.role]}")
            if p.role == Role.WEREWOLF:
                mates = ", ".join(w.name for w in wolves if w.id != p.id) or "không có"
                p.private_log.append(f"🐺 Đồng đội sói: {mates}")
        self.task = asyncio.create_task(self._run())

    def reset_to_lobby(self) -> None:
        if self.phase != Phase.ENDED:
            raise GameError("Ván chưa kết thúc")
        self._reset_game()
        for p in self.players.values():
            p.role, p.alive, p.private_log = None, True, []
        self._auto_roles()

    # ------------------------------------------------------------ tiện ích
    def alive(self, role: Role | None = None) -> list[Player]:
        return [p for p in self.players.values()
                if p.alive and (role is None or p.role == role)]

    def _name(self, pid: str | None) -> str:
        return self.players[pid].name if pid in self.players else "?"

    def _event(self, text: str) -> None:
        self.log.append({"t": time.time(), "text": text})

    def _kill(self, pid: str) -> None:
        p = self.players[pid]
        p.alive = False
        if self.settings.reveal_role_on_death:
            self._event(f"🪦 {p.name} là {ROLE_VI[p.role]}")

    def _need_alive(self, target: str | None) -> None:
        if not target or target not in self.players or not self.players[target].alive:
            raise GameError("Mục tiêu không hợp lệ")

    def _check_win(self) -> bool:
        wolves = len(self.alive(Role.WEREWOLF))
        others = len(self.alive()) - wolves
        if wolves == 0:
            self.winner = "village"
        elif wolves >= others:
            self.winner = "wolves"
        return self.winner is not None

    # ------------------------------------------------------------ vòng lặp ván
    async def _run(self) -> None:
        try:
            self._event("🌙 Ván chơi bắt đầu. Màn đêm buông xuống…")
            while True:
                self.day_no += 1
                await self._night()
                if await self._dawn():
                    return
                if await self._day():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # không để lỗi làm treo phòng mà không ai biết
            log.exception("Game loop crashed in room %s", self.code)
            self._event("⚠️ Lỗi máy chủ – ván chơi đã dừng.")
            self.phase, self.deadline = Phase.ENDED, None
            await self._broadcast(self)

    async def _step(self, phase: Phase, seconds: float, actors: set[str],
                    hide_empty: bool = True) -> None:
        """Mở một bước, chờ người hành động xong hoặc hết giờ."""
        self.phase = phase
        self.actors = set(actors)
        self._step_done = asyncio.Event()
        self.deadline = time.time() + seconds
        await self._broadcast(self)
        # Vai đã chết vẫn "thức dậy" một lúc để không lộ ai đã chết.
        wait = seconds if (actors or not hide_empty) else random.uniform(*self.fake_wait)
        try:
            await asyncio.wait_for(self._step_done.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        self.actors = set()

    def _finish_step(self) -> None:
        self.actors = set()
        self._step_done.set()

    async def _announce(self, phase: Phase) -> None:
        self.phase, self.deadline, self.actors = phase, None, set()
        await self._broadcast(self)
        await asyncio.sleep(self.announce_seconds)

    async def _night(self) -> None:
        self._reset_night()
        self._event(f"🌙 Đêm {self.day_no}")
        s = self.settings.night_step_seconds
        ids = lambda role: {p.id for p in self.alive(role)}

        await self._step(Phase.NIGHT_GUARD, s, ids(Role.BODYGUARD))
        await self._step(Phase.NIGHT_WOLVES, s, ids(Role.WEREWOLF))
        votes = Counter(self.wolf_votes.values())
        if votes:  # bầy chưa thống nhất -> theo số đông, hoà thì bốc thăm
            top = max(votes.values())
            self.wolf_target = random.choice([t for t, n in votes.items() if n == top])
        await self._step(Phase.NIGHT_SEER, s, ids(Role.SEER))
        has_potion = self.witch_heal_left or self.witch_poison_left
        await self._step(Phase.NIGHT_WITCH, s, ids(Role.WITCH) if has_potion else set())

    async def _dawn(self) -> bool:
        deaths: dict[str, str] = {}
        t = self.wolf_target
        if t and t != self.guard_target and not self.witch_heal:
            deaths[t] = "wolf"
        if self.witch_poison and self.witch_poison not in deaths:
            deaths[self.witch_poison] = "poison"
        self.last_guarded = self.guard_target

        order = list(deaths)
        random.shuffle(order)  # không lộ ai chết vì sói, ai chết vì độc
        if order:
            names = ", ".join(self._name(d) for d in order)
            self._event(f"☀️ Trời sáng. Đêm qua {names} đã chết.")
        else:
            self._event("☀️ Trời sáng. Đêm qua bình yên, không ai chết.")
        for pid in order:
            self._kill(pid)
        await self._announce(Phase.DAWN)

        for pid in order:  # Thợ săn bắn trước khi xét thắng thua
            if self.players[pid].role == Role.HUNTER and deaths[pid] != "poison":
                await self._hunter(pid)
        if self._check_win():
            await self._end()
            return True
        return False

    async def _day(self) -> bool:
        await self._step(Phase.DAY_DISCUSS, self.settings.discuss_seconds, set(),
                         hide_empty=False)
        self.day_votes = {}
        await self._step(Phase.DAY_VOTE, self.settings.vote_seconds,
                         {p.id for p in self.alive()})
        target = self._vote_result()
        if target:
            self._event(f"⚖️ Dân làng đã treo cổ {self._name(target)}.")
            self._kill(target)
        else:
            self._event("⚖️ Phiếu không đủ – hôm nay không ai bị treo cổ.")
        await self._announce(Phase.VERDICT)
        if target and self.players[target].role == Role.HUNTER:
            await self._hunter(target)
        if self._check_win():
            await self._end()
            return True
        return False

    def _vote_result(self) -> str | None:
        c = Counter(self.day_votes.values())
        skips = c.pop("skip", 0)
        if not c:
            return None
        top = max(c.values())
        leaders = [t for t, n in c.items() if n == top]
        if len(leaders) > 1 or skips >= top:  # hoà, hoặc phiếu trắng thắng
            return None
        return leaders[0]

    async def _hunter(self, pid: str) -> None:
        self.hunter_shooter, self.hunter_target = pid, None
        self._event(f"🏹 {self._name(pid)} là Thợ săn và được bắn một phát cuối!")
        await self._step(Phase.HUNTER_SHOT, self.settings.hunter_seconds, {pid},
                         hide_empty=False)
        t = self.hunter_target
        if t and self.players[t].alive:
            self._event(f"💥 Thợ săn đã bắn {self._name(t)}.")
            self._kill(t)
        else:
            self._event("Thợ săn đã hạ súng, không bắn ai.")
        self.hunter_shooter = None
        await self._announce(Phase.VERDICT)

    async def _end(self) -> None:
        self.phase, self.deadline, self.actors = Phase.ENDED, None, set()
        self._event("🏆 Phe Dân làng chiến thắng!" if self.winner == "village"
                    else "🐺 Phe Sói chiến thắng!")
        await self._broadcast(self)

    # ------------------------------------------------------------ hành động
    async def handle_action(self, pid: str, action: str, target: str | None = None) -> None:
        p = self.players.get(pid)
        if p is None:
            raise GameError("Người chơi không tồn tại")
        ph = self.phase

        if action == "skip_discussion":
            if not p.is_host or ph != Phase.DAY_DISCUSS:
                raise GameError("Chỉ chủ phòng mới bỏ qua được thảo luận")
            self._finish_step()
            return
        if pid not in self.actors:
            raise GameError("Chưa đến lượt của bạn")

        if ph == Phase.NIGHT_GUARD and action == "guard":
            self._need_alive(target)
            if target == self.last_guarded:
                raise GameError("Không được bảo vệ một người hai đêm liền")
            self.guard_target = target
            p.private_log.append(f"🛡️ Đêm {self.day_no}: bạn bảo vệ {self._name(target)}")
            self._finish_step()

        elif ph == Phase.NIGHT_WOLVES and action == "wolf_vote":
            self._need_alive(target)
            if self.players[target].role == Role.WEREWOLF:
                raise GameError("Không thể cắn đồng đội")
            self.wolf_votes[pid] = target
            pack = {w.id for w in self.alive(Role.WEREWOLF)}
            if pack <= self.wolf_votes.keys() and len(set(self.wolf_votes.values())) == 1:
                self._finish_step()  # cả bầy đã thống nhất

        elif ph == Phase.NIGHT_SEER and action == "seer_check":
            if self.seer_result:
                raise GameError("Bạn đã soi rồi")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự soi mình")
            wolf = self.players[target].role == Role.WEREWOLF
            self.seer_result = f"{self._name(target)} {'LÀ SÓI 🐺' if wolf else 'không phải sói'}"
            p.private_log.append(f"🔮 Đêm {self.day_no}: {self.seer_result}")
        elif ph == Phase.NIGHT_SEER and action == "seer_ack":
            self._finish_step()

        elif ph == Phase.NIGHT_WITCH and action == "witch_heal":
            if not self.witch_heal_left or not self.wolf_target:
                raise GameError("Không thể dùng bình cứu lúc này")
            self.witch_heal, self.witch_heal_left = True, False
            p.private_log.append(f"🧪 Đêm {self.day_no}: bạn cứu {self._name(self.wolf_target)}")
        elif ph == Phase.NIGHT_WITCH and action == "witch_poison":
            if not self.witch_poison_left:
                raise GameError("Bạn đã dùng bình độc")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự đầu độc")
            self.witch_poison, self.witch_poison_left = target, False
            p.private_log.append(f"☠️ Đêm {self.day_no}: bạn đầu độc {self._name(target)}")
        elif ph == Phase.NIGHT_WITCH and action == "witch_done":
            self._finish_step()

        elif ph == Phase.DAY_VOTE and action == "vote":
            if target != "skip":
                self._need_alive(target)
                if target == pid:
                    raise GameError("Không thể bỏ phiếu cho chính mình")
            self.day_votes[pid] = target
            if self.actors <= self.day_votes.keys():
                self._finish_step()

        elif ph == Phase.HUNTER_SHOT and action in ("hunter_shoot", "hunter_skip"):
            if action == "hunter_shoot":
                self._need_alive(target)
                if target == pid:
                    raise GameError("Hãy chọn người khác")
                self.hunter_target = target
            self._finish_step()
        else:
            raise GameError("Hành động không hợp lệ lúc này")
        await self._broadcast(self)

    # ------------------------------------------------------------ góc nhìn riêng
    def view_for(self, pid: str) -> dict:
        me = self.players[pid]
        started = self.phase != Phase.LOBBY
        ended = self.phase == Phase.ENDED

        def visible_role(o: Player) -> str | None:
            if not started or o.role is None:
                return None
            if (ended or o.id == pid
                    or (not o.alive and self.settings.reveal_role_on_death)
                    or (me.role == Role.WEREWOLF and o.role == Role.WEREWOLF)):
                return o.role.value
            return None

        return {
            "type": "state",
            "code": self.code,
            "phase": self.phase.value,
            "day": self.day_no,
            "deadline": self.deadline,
            "server_now": time.time(),
            "me": {"id": me.id, "name": me.name, "is_host": me.is_host,
                   "alive": me.alive, "role": me.role.value if me.role else None},
            "players": [{"id": o.id, "name": o.name, "alive": o.alive,
                         "connected": o.connected, "is_host": o.is_host,
                         "role": visible_role(o)} for o in self.players.values()],
            "settings": asdict(self.settings),
            "roles_custom": self.roles_custom,
            "limits": {"min": MIN_PLAYERS, "max": MAX_PLAYERS},
            "log": self.log[-40:],
            "private": me.private_log[-20:],
            "action": self._action_for(me),
            "votes": self.day_votes if self.phase in (Phase.DAY_VOTE, Phase.VERDICT) else {},
            "hunter": self.hunter_shooter,
            "winner": self.winner,
        }

    def _action_for(self, me: Player) -> dict | None:
        if me.id not in self.actors:
            return None
        others = [p.id for p in self.alive() if p.id != me.id]
        ph = self.phase
        if ph == Phase.NIGHT_GUARD:
            return {"kind": "guard", "last": self.last_guarded,
                    "targets": [p.id for p in self.alive() if p.id != self.last_guarded]}
        if ph == Phase.NIGHT_WOLVES:
            return {"kind": "wolf_vote", "chosen": self.wolf_votes.get(me.id),
                    "team_votes": self.wolf_votes,
                    "targets": [p.id for p in self.alive() if p.role != Role.WEREWOLF]}
        if ph == Phase.NIGHT_SEER:
            return {"kind": "seer_check", "result": self.seer_result, "targets": others}
        if ph == Phase.NIGHT_WITCH:
            return {"kind": "witch",
                    "victim": self.wolf_target if (self.witch_heal_left or self.witch_heal) else None,
                    "heal_left": self.witch_heal_left, "healed": self.witch_heal,
                    "poison_left": self.witch_poison_left, "poisoned": self.witch_poison,
                    "targets": others}
        if ph == Phase.DAY_VOTE:
            return {"kind": "vote", "chosen": self.day_votes.get(me.id), "targets": others}
        if ph == Phase.HUNTER_SHOT:
            return {"kind": "hunter", "targets": others}
        return None
