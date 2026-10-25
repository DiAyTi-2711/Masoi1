"""Ma Sói – engine trò chơi (16 vai) có máy quản trò đọc lời dẫn.

Không phụ thuộc FastAPI để dễ test. Toàn bộ trạng thái của một phòng nằm trong
`Room`; mọi thay đổi chạy trên một event loop asyncio duy nhất nên không cần lock.
Mỗi khi trạng thái đổi, engine gọi `broadcast(room)` để lớp WebSocket gửi cho
từng người một "góc nhìn" riêng (view_for).

Máy quản trò (narrator) là một thiết bị không chơi: nhận danh sách câu thoại
`speech` và đọc to bằng giọng tiếng Việt, kể cả bản tổng kết diễn biến cuối ván.
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
    # Phe Dân Làng
    VILLAGER = "villager"
    BODYGUARD = "bodyguard"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    DISEASED = "diseased"        # bị sói cắn chết -> đêm sau bầy sói không cắn được
    MOON_MAIDEN = "moon_maiden"  # mỗi đêm: soi vai 1 người HOẶC che chắn 1 người khỏi kỹ năng sói
    SPELLCASTER = "spellcaster"  # mỗi đêm làm 1 người bị câm trong buổi thảo luận hôm sau
    # Phe Ma Sói
    WEREWOLF = "werewolf"
    MUTANT_WOLF = "mutant_wolf"  # đêm 1 là dân, từ đêm 2 hoá sói
    WOLF_CUB = "wolf_cub"        # chết -> đêm sau bầy được cắn 2 người
    SNOW_WOLF = "snow_wolf"      # phong ấn năng lực 1 người trong đêm kế tiếp
    # Phe thứ ba & biến đổi
    CURSED = "cursed"            # bị sói cắn -> hoá sói thay vì chết
    TRAITOR = "traitor"          # trông như dân, thắng cùng sói; sói chết hết thì hoá sói
    CLONER = "cloner"            # đêm 1 chọn mục tiêu; mục tiêu chết -> nhận vai & phe
    CREATOR = "creator"          # xem 2 lá dư, chọn 1 làm vai của mình


ROLE_VI = {
    Role.VILLAGER: "Dân Làng", Role.BODYGUARD: "Bảo Vệ", Role.SEER: "Tiên Tri",
    Role.WITCH: "Phù Thủy", Role.HUNTER: "Thợ Săn", Role.DISEASED: "Người Bệnh",
    Role.MOON_MAIDEN: "Nguyệt Nữ", Role.SPELLCASTER: "Kẻ Phù Phép",
    Role.WEREWOLF: "Ma Sói", Role.MUTANT_WOLF: "Sói Biến Chủng", Role.WOLF_CUB: "Sói Con",
    Role.SNOW_WOLF: "Sói Băng", Role.CURSED: "Kẻ Bị Nguyền", Role.TRAITOR: "Kẻ Phản Bội",
    Role.CLONER: "Kẻ Nhân Bản", Role.CREATOR: "Kẻ Sáng Tạo",
}
WOLF_CARDS = {Role.WEREWOLF, Role.MUTANT_WOLF, Role.WOLF_CUB, Role.SNOW_WOLF}
SINGLE_ROLES = set(Role) - {Role.WEREWOLF, Role.VILLAGER}  # mỗi vai đặc biệt tối đa 1
CREATOR_EXTRA = 2  # số lá dư khi có Kẻ Sáng Tạo


class Phase(str, Enum):
    LOBBY = "lobby"
    NIGHTFALL = "nightfall"
    NIGHT_CREATOR = "night_creator"
    NIGHT_CLONER = "night_cloner"
    NIGHT_GUARD = "night_guard"
    NIGHT_WOLVES = "night_wolves"
    NIGHT_WITCH = "night_witch"
    NIGHT_SEER = "night_seer"
    NIGHT_MOON = "night_moon"
    NIGHT_SNOW = "night_snow"
    NIGHT_SPELL = "night_spell"
    DAWN = "dawn"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    VERDICT = "verdict"
    HUNTER_SHOT = "hunter_shot"
    ENDED = "ended"


# Bị Sói Băng phong ấn thì mất lượt ở các bước này
FREEZABLE = {Phase.NIGHT_GUARD, Phase.NIGHT_WITCH, Phase.NIGHT_SEER,
             Phase.NIGHT_MOON, Phase.NIGHT_SPELL}

# Câu gọi vai của quản trò
CALLS = {
    Phase.NIGHT_CREATOR: (Role.CREATOR, "Kẻ Sáng Tạo hãy thức dậy. Hai lá bài dư đang hiện trên điện thoại của bạn, hãy chọn một lá làm vai của mình."),
    Phase.NIGHT_CLONER: (Role.CLONER, "Kẻ Nhân Bản hãy thức dậy và chọn người bạn muốn nhân bản."),
    Phase.NIGHT_GUARD: (Role.BODYGUARD, "Bảo Vệ hãy thức dậy và chọn người cần bảo vệ đêm nay."),
    Phase.NIGHT_WOLVES: (Role.WEREWOLF, "Ma Sói hãy thức dậy, nhìn nhau và chọn người để cắn."),
    Phase.NIGHT_WITCH: (Role.WITCH, "Phù Thủy hãy thức dậy. Bạn có muốn dùng bình cứu hay bình độc không?"),
    Phase.NIGHT_SEER: (Role.SEER, "Tiên Tri hãy thức dậy và chọn người bạn muốn soi."),
    Phase.NIGHT_MOON: (Role.MOON_MAIDEN, "Nguyệt Nữ hãy thức dậy. Hãy soi một người, hoặc che chắn một người."),
    Phase.NIGHT_SNOW: (Role.SNOW_WOLF, "Sói Băng hãy thức dậy và chọn người để phong ấn."),
    Phase.NIGHT_SPELL: (Role.SPELLCASTER, "Kẻ Phù Phép hãy thức dậy và chọn người sẽ bị câm vào ngày mai."),
}


def suggest_roles(n: int) -> dict[str, int]:
    """Chia vai mặc định: ~1/4 là sói; càng đông càng thêm vai đặc biệt."""
    r = {role: 0 for role in Role}
    wolves = max(1, n // 4)
    r[Role.WOLF_CUB] = int(n >= 12)
    r[Role.SNOW_WOLF] = int(n >= 16)
    r[Role.MUTANT_WOLF] = int(n >= 20)
    r[Role.WEREWOLF] = wolves - r[Role.WOLF_CUB] - r[Role.SNOW_WOLF] - r[Role.MUTANT_WOLF]
    r[Role.SEER] = r[Role.BODYGUARD] = r[Role.WITCH] = 1
    r[Role.HUNTER] = int(n >= 8)
    r[Role.SPELLCASTER] = int(n >= 12)
    r[Role.DISEASED] = r[Role.CURSED] = int(n >= 14)
    r[Role.MOON_MAIDEN] = int(n >= 16)
    r[Role.TRAITOR] = int(n >= 18)
    r[Role.CLONER] = int(n >= 20)
    r[Role.CREATOR] = int(n >= 22)
    out = {role.value: c for role, c in r.items()}
    _fill_villagers(out, n)
    return out


def _fill_villagers(r: dict[str, int], n: int) -> None:
    extra = CREATOR_EXTRA if r.get(Role.CREATOR.value) else 0
    others = sum(c for k, c in r.items() if k != Role.VILLAGER.value)
    r[Role.VILLAGER.value] = max(0, n + extra - others)


@dataclass
class Player:
    id: str
    name: str
    token: str
    is_host: bool = False
    narrator: bool = False     # máy quản trò: không nhận vai, không chơi
    role: Optional[Role] = None
    alive: bool = True
    connected: bool = False
    private_log: list[str] = field(default_factory=list)


@dataclass
class Settings:
    roles: dict[str, int] = field(default_factory=dict)
    creator_seconds: int = 30
    wolves_seconds: int = 20
    role_seconds: int = 5        # mọi vai còn lại
    hunter_seconds: int = 15
    discuss_seconds: int = 180
    vote_seconds: int = 60
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
        # Thời gian để quản trò đọc câu gọi trước khi đếm giờ của vai (test có thể chỉnh nhỏ)
        self.call_seconds = 5.0
        self.announce_seconds = 6.0
        self._speech_id = 0
        self.task: asyncio.Task | None = None
        self._reset_game()

    # ------------------------------------------------------------ phòng chờ
    def _reset_game(self) -> None:
        self.phase = Phase.LOBBY
        self.day_no = 0
        self.deadline: float | None = None
        self.log: list[dict] = []
        self.speech: list[dict] = []
        self.history: list[dict] = []
        self._period = "Chia bài"
        self.winner: str | None = None
        self.actors: set[str] = set()
        self._step_done = asyncio.Event()
        self.last_guarded: str | None = None
        self.witch_heal_left = True
        self.witch_poison_left = True
        self.day_votes: dict[str, str] = {}
        self.hunter_shooter: str | None = None
        self.hunter_target: str | None = None
        self.creator_options: list[Role] = []
        self.creator_choice: Role | None = None
        self.clone_target: str | None = None
        self.sick_next = False      # Người Bệnh vừa bị cắn chết
        self.cub_bonus = False      # Sói Con vừa chết
        self.frozen_next: str | None = None
        self.silenced: str | None = None
        self._reset_night()

    def _reset_night(self) -> None:
        self.moon_result: str | None = None
        self.moon_shield: str | None = None
        self.frozen: str | None = None
        self.guard_target: str | None = None
        self.bites = 1
        self.wolf_votes: dict[str, list[str]] = {}
        self.wolf_targets: list[str] = []
        self.seer_result: str | None = None
        self.witch_heal: str | None = None
        self.witch_poison: str | None = None
        self.spell_target: str | None = None

    def seated(self) -> list[Player]:
        """Người chơi thật (không tính máy quản trò)."""
        return [p for p in self.players.values() if not p.narrator]

    @property
    def narrator(self) -> Player | None:
        return next((p for p in self.players.values() if p.narrator), None)

    def add_player(self, name: str, is_host: bool = False, narrator: bool = False) -> Player:
        if self.phase != Phase.LOBBY:
            raise GameError("Ván đã bắt đầu, không thể vào thêm")
        if not narrator and len(self.seated()) >= MAX_PLAYERS:
            raise GameError(f"Phòng đã đủ {MAX_PLAYERS} người")
        name = " ".join(name.split())[:20] or ("Quản trò" if narrator else "")
        if not name:
            raise GameError("Hãy nhập tên")
        if any(p.name.lower() == name.lower() for p in self.players.values()):
            raise GameError("Tên này đã có người dùng trong phòng")
        p = Player(id=secrets.token_hex(4), name=name, token=secrets.token_urlsafe(16),
                   is_host=is_host, narrator=narrator)
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
        """Tự động: chia theo số người. Tuỳ chỉnh: giữ vai Host chọn, Dân Làng tự lấp chỗ trống."""
        n = len(self.seated())
        if not self.roles_custom:
            self.settings.roles = suggest_roles(n)
        else:
            _fill_villagers(self.settings.roles, n)

    def update_settings(self, data: dict) -> None:
        if self.phase != Phase.LOBBY:
            raise GameError("Chỉ đổi cấu hình được ở phòng chờ")
        try:
            if data.get("auto_roles"):
                self.roles_custom = False
            elif isinstance(data.get("roles"), dict):
                self.settings.roles = {
                    r.value: max(0, min(1 if r in SINGLE_ROLES else MAX_PLAYERS,
                                        int(data["roles"].get(r.value, 0))))
                    for r in Role
                }
                self.roles_custom = True
            self._auto_roles()
            limits = {"creator_seconds": (10, 90), "wolves_seconds": (10, 90),
                      "role_seconds": (5, 60), "hunter_seconds": (5, 60),
                      "discuss_seconds": (30, 900), "vote_seconds": (15, 300)}
            for key, (lo, hi) in limits.items():
                if key in data:
                    setattr(self.settings, key, max(lo, min(hi, int(data[key]))))
            if "reveal_role_on_death" in data:
                self.settings.reveal_role_on_death = bool(data["reveal_role_on_death"])
        except (TypeError, ValueError):
            raise GameError("Cấu hình không hợp lệ")

    def validate_roles(self) -> None:
        n, r = len(self.seated()), self.settings.roles
        if n < MIN_PLAYERS:
            raise GameError(f"Cần ít nhất {MIN_PLAYERS} người chơi")
        need = n + (CREATOR_EXTRA if r.get(Role.CREATOR.value) else 0)
        total = sum(r.values())
        if total != need:
            raise GameError(f"Đang có {total} lá cho {need} chỗ – hãy bớt vai đặc biệt")
        wolves = sum(r.get(w.value, 0) for w in WOLF_CARDS)
        if wolves < 1:
            raise GameError("Cần ít nhất 1 lá phe Sói")
        if wolves >= n - wolves:
            raise GameError("Sói quá đông – phải ít hơn phe Dân")

    async def start(self) -> None:
        self.validate_roles()
        players = self.seated()
        cards = [Role(k) for k, c in self.settings.roles.items() for _ in range(c)]
        has_creator = Role.CREATOR in cards
        if has_creator:
            cards.remove(Role.CREATOR)
        for _ in range(200):  # đảm bảo sau khi rút 2 lá dư vẫn còn sói trong ván
            random.shuffle(cards)
            extra = cards[:CREATOR_EXTRA] if has_creator else []
            deal = cards[len(extra):]
            if any(c in WOLF_CARDS for c in deal):
                break
        random.shuffle(players)
        self._reset_game()
        self.creator_options = extra
        if has_creator:
            deal = [Role.CREATOR] + deal
        for p, role in zip(players, deal):
            p.role, p.alive, p.private_log = role, True, []
        self._hist("Bài đã chia: " + ", ".join(f"{p.name} – {ROLE_VI[p.role]}" for p in players)
                   + (f". Hai lá dư: {ROLE_VI[extra[0]]}, {ROLE_VI[extra[1]]}" if extra else ""))
        self.task = asyncio.create_task(self._run())

    def reset_to_lobby(self) -> None:
        if self.phase != Phase.ENDED:
            raise GameError("Ván chưa kết thúc")
        self._reset_game()
        for p in self.players.values():
            p.role, p.alive, p.private_log = None, True, []
        self._auto_roles()

    # ------------------------------------------------------------ tiện ích
    def is_wolf(self, p: Player) -> bool:
        """Sói Biến Chủng chỉ thành sói từ đêm thứ 2."""
        if p.role == Role.MUTANT_WOLF:
            return self.day_no >= 2
        return p.role in WOLF_CARDS

    def alive(self, role: Role | None = None) -> list[Player]:
        return [p for p in self.seated()
                if p.alive and (role is None or p.role == role)]

    def wolves(self) -> list[Player]:
        return [p for p in self.alive() if self.is_wolf(p)]

    def _names(self, ids) -> str:
        names = [self._name(i) for i in ids]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " và " + names[-1]

    def _pack_names(self, exclude: str) -> str:
        return ", ".join(w.name for w in self.wolves() if w.id != exclude) or "không còn ai"

    def _holders(self, role: Role) -> str:
        return ", ".join(p.name for p in self.seated() if p.role == role)

    def _in_game(self, role: Role) -> bool:
        return self.settings.roles.get(role.value, 0) > 0

    def _name(self, pid: str | None) -> str:
        return self.players[pid].name if pid in self.players else "?"

    def _event(self, text: str) -> None:
        self.log.append({"t": time.time(), "text": text})

    def _say(self, text: str) -> None:
        """Câu để máy quản trò đọc to."""
        self._speech_id += 1
        self.speech.append({"id": self._speech_id, "text": text})

    def _hist(self, text: str) -> None:
        """Diễn biến bí mật – chỉ công bố khi ván kết thúc."""
        self.history.append({"period": self._period, "text": text})

    def _note(self, pid: str, text: str) -> None:
        self.players[pid].private_log.append(text)

    def _join_pack(self, p: Player, reason: str) -> None:
        self._note(p.id, f"{reason} Đồng đội sói: {self._pack_names(p.id)}")
        for w in self.wolves():
            if w.id != p.id:
                self._note(w.id, f"🐺 {p.name} vừa gia nhập bầy sói.")

    def _kill(self, pid: str, cause: str) -> list[tuple[str, str]]:
        p = self.players[pid]
        if not p.alive:
            return []
        p.alive = False
        if self.settings.reveal_role_on_death:
            self._event(f"🪦 {p.name} là {ROLE_VI[p.role]}")
        if p.role == Role.WOLF_CUB:
            self.cub_bonus = True
        for c in self.alive(Role.CLONER):  # Kẻ Nhân Bản nhận lá bài của mục tiêu
            if self.clone_target == pid:
                c.role = p.role
                self._note(c.id, f"🧬 {p.name} đã chết. Bạn nhận lá {ROLE_VI[p.role]}.")
                self._hist(f"Kẻ Nhân Bản {c.name} nhận lá {ROLE_VI[p.role]} của {p.name}.")
                if self.is_wolf(c):
                    self._join_pack(c, "🐺 Bạn đã thuộc phe Sói.")
        return [(pid, cause)]

    def _need_alive(self, target) -> None:
        if (not isinstance(target, str) or target not in self.players
                or self.players[target].narrator or not self.players[target].alive):
            raise GameError("Mục tiêu không hợp lệ")

    def _check_win(self) -> bool:
        if self.winner:
            return True
        alive = self.alive()
        wolves = [p for p in alive if self.is_wolf(p)]
        if not wolves:
            traitor = next((p for p in alive if p.role == Role.TRAITOR), None)
            if traitor:  # Kẻ Phản Bội trở thành con sói cuối cùng
                traitor.role = Role.WEREWOLF
                self._note(traitor.id, "🗡️ Toàn bộ sói đã chết. Bạn lộ nanh vuốt – giờ bạn là Ma Sói duy nhất!")
                self._hist(f"Sói đã chết hết, Kẻ Phản Bội {traitor.name} hoá thành Ma Sói.")
                wolves = [traitor]
            elif any(p.role == Role.MUTANT_WOLF for p in alive):
                return False  # Sói Biến Chủng sắp hoá sói
            else:
                self.winner = "village"
                return True
        if len(wolves) >= len(alive) - len(wolves):
            self.winner = "wolves"
        return self.winner is not None

    # ------------------------------------------------------------ vòng lặp ván
    async def _run(self) -> None:
        try:
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
            self._say("Xin lỗi, máy chủ gặp lỗi. Ván chơi đã dừng.")
            self.phase, self.deadline = Phase.ENDED, None
            await self._broadcast(self)

    def _intros(self) -> None:
        tips = {
            Role.MUTANT_WOLF: "🌀 Đêm đầu bạn là Dân Làng. Từ đêm thứ 2 bạn hoá sói và thức dậy cùng bầy.",
            Role.TRAITOR: "🗡️ Bạn trông như Dân Làng nhưng thắng cùng phe Sói. Khi sói chết hết, bạn hoá sói.",
            Role.CLONER: "🧬 Đêm đầu hãy chọn một người. Khi họ chết, bạn nhận lá bài và phe của họ.",
            Role.CURSED: "🌗 Nếu bị sói cắn, bạn không chết mà hoá thành sói.",
            Role.WOLF_CUB: "🐾 Nếu bạn chết, đêm sau bầy được cắn 2 người.",
            Role.DISEASED: "🤒 Nếu sói cắn chết bạn, đêm sau bầy sói không cắn được ai.",
            Role.HUNTER: "🏹 Nếu chết trong đêm, bạn được kéo một người chết theo.",
        }
        for p in self.seated():
            p.private_log.insert(0, f"Vai của bạn: {ROLE_VI[p.role]}")
            if p.role in tips:
                self._note(p.id, tips[p.role])
            if self.is_wolf(p):
                self._note(p.id, f"🐺 Đồng đội sói: {self._pack_names(p.id)}")

    async def _step(self, phase: Phase, seconds: float, actors: set[str],
                    early: bool = False) -> None:
        """Mở một bước. early=False: luôn chờ đủ giờ để không lộ ai đã hành động/đã chết."""
        self.phase = phase
        self.actors = set(actors)
        self._step_done = asyncio.Event()
        self.deadline = time.time() + seconds
        await self._broadcast(self)
        try:
            if early:
                await asyncio.wait_for(self._step_done.wait(), timeout=seconds)
            else:
                await asyncio.sleep(seconds)
        except asyncio.TimeoutError:
            pass
        self.actors = set()

    async def _call(self, phase: Phase, seconds: float, actors: set[str],
                    early: bool = False) -> None:
        """Quản trò gọi một vai dậy, chờ, rồi cho vai đó đi ngủ."""
        role, text = CALLS[phase]
        self._say(text)
        await self._step(phase, self.call_seconds + seconds, actors, early)
        self._say(f"{ROLE_VI[role]} hãy nhắm mắt lại và đi ngủ.")

    def _finish_step(self) -> None:
        self.actors = set()
        self._step_done.set()

    def _done(self, pid: str) -> None:
        self.actors.discard(pid)
        if not self.actors:
            self._step_done.set()

    async def _announce(self, phase: Phase, seconds: float | None = None) -> None:
        self.phase, self.deadline, self.actors = phase, None, set()
        await self._broadcast(self)
        await asyncio.sleep(self.announce_seconds if seconds is None else seconds)

    async def _night(self) -> None:
        self._reset_night()
        self._period = f"Đêm {self.day_no}"
        if self.day_no == 2:
            for m in self.alive(Role.MUTANT_WOLF):
                self._join_pack(m, "🌀 Bạn đã hoá thành sói!")
                self._hist(f"Sói Biến Chủng {m.name} hoá sói.")
        self.frozen, self.frozen_next = self.frozen_next, None
        if self.frozen and not self.players[self.frozen].alive:
            self.frozen = None
        if self.frozen:
            self._note(self.frozen, f"❄️ Đêm {self.day_no}: bạn bị Sói Băng phong ấn, không dùng được năng lực.")
            self._hist(f"{self._name(self.frozen)} bị phong ấn suốt đêm nay.")

        self._event(f"🌙 Đêm {self.day_no}")
        self._say(f"Đêm thứ {self.day_no}. Màn đêm buông xuống, tất cả mọi người hãy nhắm mắt lại.")
        await self._announce(Phase.NIGHTFALL)

        st = self.settings
        ids = lambda role: {p.id for p in self.alive(role)}
        # Gọi MỌI vai có trong cấu hình, kể cả đã chết hay là lá dư – luôn chờ đủ giờ.
        if self.day_no == 1:
            if self._in_game(Role.CREATOR):
                creator = next((p for p in self.seated() if p.role == Role.CREATOR), None)
                await self._call(Phase.NIGHT_CREATOR, st.creator_seconds,
                                 {creator.id} if creator else set())
                if creator:
                    choice = self.creator_choice or random.choice(self.creator_options)
                    other = [r for r in self.creator_options if r != choice] or [choice]
                    creator.role = choice
                    self._note(creator.id, f"✨ Bạn là Kẻ Sáng Tạo và đã chọn vai {ROLE_VI[choice]}.")
                    self._hist(f"Kẻ Sáng Tạo {creator.name} chọn lá {ROLE_VI[choice]}, bỏ lá {ROLE_VI[other[0]]}"
                               + ("" if self.creator_choice else " (hết giờ, máy chọn ngẫu nhiên)") + ".")
            self._intros()
            if self._in_game(Role.CLONER):
                await self._call(Phase.NIGHT_CLONER, st.role_seconds, ids(Role.CLONER))
        if self._in_game(Role.BODYGUARD):
            await self._call(Phase.NIGHT_GUARD, st.role_seconds, ids(Role.BODYGUARD))

        self.bites = 0 if self.sick_next else (2 if self.cub_bonus else 1)
        self.sick_next = False
        if self.bites:
            self.cub_bonus = False
        pack = self.wolves()
        await self._call(Phase.NIGHT_WOLVES, st.wolves_seconds, {w.id for w in pack})
        votes = Counter(t for ts in self.wolf_votes.values() for t in ts)
        for _ in range(self.bites):  # theo số đông, hoà thì bốc thăm
            if not votes:
                break
            top = max(votes.values())
            pick = random.choice([t for t, c in votes.items() if c == top])
            self.wolf_targets.append(pick)
            del votes[pick]
        who = ", ".join(w.name for w in pack) or "không còn con nào"
        if self.bites == 0:
            self._hist(f"Bầy sói ({who}) nhiễm bệnh nên không cắn được ai.")
        elif self.wolf_targets:
            self._hist(f"Bầy sói ({who}) chọn cắn {self._names(self.wolf_targets)}.")
        else:
            self._hist(f"Bầy sói ({who}) không chọn ai.")

        if self._in_game(Role.WITCH):
            await self._call(Phase.NIGHT_WITCH, st.role_seconds, ids(Role.WITCH))
        if self._in_game(Role.SEER):
            await self._call(Phase.NIGHT_SEER, st.role_seconds, ids(Role.SEER))
        for phase, role in ((Phase.NIGHT_MOON, Role.MOON_MAIDEN), (Phase.NIGHT_SNOW, Role.SNOW_WOLF),
                            (Phase.NIGHT_SPELL, Role.SPELLCASTER)):
            if self._in_game(role):
                await self._call(phase, st.role_seconds, ids(role))

    async def _dawn(self) -> bool:
        protected = {x: by for x, by in ((self.guard_target, "Bảo Vệ"), (self.moon_shield, "Nguyệt Nữ")) if x}
        deaths: dict[str, str] = {}
        for t in self.wolf_targets:
            if t in protected:
                self._hist(f"{self._name(t)} được {protected[t]} che chở nên thoát chết.")
                if self.witch_heal == t:  # cứu người đã được bảo vệ -> hoàn bình
                    self.witch_heal_left = True
                    self._witch_note(f"🧪 {self._name(t)} đã được bảo vệ sẵn – bình cứu được hoàn lại.")
                continue
            if self.witch_heal == t:
                self._hist(f"{self._name(t)} được Phù Thủy cứu sống.")
                continue
            v = self.players[t]
            if v.role == Role.CURSED:
                v.role = Role.WEREWOLF
                self._join_pack(v, "🌗 Bạn bị sói cắn và đã hoá thành Ma Sói!")
                self._hist(f"Kẻ Bị Nguyền {v.name} bị cắn và hoá thành Ma Sói.")
            else:
                deaths[t] = "wolf"
        if self.witch_poison:
            if self.witch_poison in deaths:  # đằng nào cũng chết -> hoàn bình
                self.witch_poison_left = True
                self._witch_note(f"☠️ {self._name(self.witch_poison)} đã chết vì sói – bình độc được hoàn lại.")
            else:
                deaths[self.witch_poison] = "poison"
        self.last_guarded = self.guard_target

        order = list(deaths)
        random.shuffle(order)  # không lộ ai chết vì sói, ai chết vì độc
        self._period = f"Ngày {self.day_no}"
        reveal = self.settings.reveal_role_on_death
        if order:
            names = self._names(order)
            self._event(f"☀️ Trời sáng. Đêm qua {names} đã chết.")
            roles = " ".join(f"{self._name(d)} là {ROLE_VI[self.players[d].role]}." for d in order) if reveal else ""
            self._say(f"Trời đã sáng, mọi người hãy mở mắt. Đêm qua, {names} đã chết. {roles}")
            for d in order:
                self._hist(f"{self._name(d)} ({ROLE_VI[self.players[d].role]}) chết vì "
                           + ("bị sói cắn." if deaths[d] == "wolf" else "bình độc của Phù Thủy."))
        else:
            self._event("☀️ Trời sáng. Đêm qua bình yên, không ai chết.")
            self._say("Trời đã sáng, mọi người hãy mở mắt. Đêm qua là một đêm bình yên, không ai chết.")
        killed = []
        for pid in order:
            role_before = self.players[pid].role
            killed += self._kill(pid, deaths[pid])
            if role_before == Role.DISEASED and deaths[pid] == "wolf" and pid != self.frozen:
                self.sick_next = True
                self._hist("Bầy sói đã cắn phải Người Bệnh – đêm sau không cắn được.")
        if self.spell_target and self.players[self.spell_target].alive:
            self.silenced = self.spell_target
            self._event(f"🤐 {self._name(self.silenced)} bị phù phép – không được nói trong buổi thảo luận hôm nay.")
            self._say(f"{self._name(self.silenced)} đã bị phù phép, hôm nay không được nói.")
        await self._announce(Phase.DAWN)

        for pid, _ in killed:  # Thợ Săn chết trong đêm được bắn
            if self.players[pid].role == Role.HUNTER:
                if pid == self.frozen:
                    self._hist(f"Thợ Săn {self._name(pid)} bị phong ấn nên không bắn được.")
                else:
                    await self._hunter(pid)
        if self._check_win():
            await self._end()
            return True
        return False

    def _witch_note(self, text: str) -> None:
        for w in self.seated():
            if w.role == Role.WITCH:
                self._note(w.id, text)

    async def _day(self) -> bool:
        mins = self.settings.discuss_seconds
        length = f"{mins // 60} phút" + (f" {mins % 60} giây" if mins % 60 else "") if mins >= 60 else f"{mins} giây"
        self._say(f"Bây giờ là thời gian thảo luận. Mọi người có {length}.")
        await self._step(Phase.DAY_DISCUSS, mins, set(), early=True)
        self.day_votes = {}
        self._say("Hết giờ thảo luận. Mọi người hãy bỏ phiếu treo cổ trên điện thoại.")
        await self._step(Phase.DAY_VOTE, self.settings.vote_seconds,
                         {p.id for p in self.alive()}, early=True)
        target = self._vote_result()
        if target:
            n = sum(1 for t in self.day_votes.values() if t == target)
            role = ROLE_VI[self.players[target].role]
            self._event(f"⚖️ Dân làng đã treo cổ {self._name(target)}.")
            self._say(f"Với {n} phiếu, {self._name(target)} đã bị treo cổ."
                      + (f" {self._name(target)} là {role}." if self.settings.reveal_role_on_death else ""))
            self._hist(f"Dân làng treo cổ {self._name(target)} ({role}) với {n} phiếu.")
            self._kill(target, "lynch")
        else:
            self._event("⚖️ Phiếu không đủ – hôm nay không ai bị treo cổ.")
            self._say("Số phiếu không đủ. Hôm nay không ai bị treo cổ.")
            self._hist("Không ai bị treo cổ.")
        self.silenced = None
        await self._announce(Phase.VERDICT)
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
        self._event(f"🏹 {self._name(pid)} là Thợ Săn và được kéo một người chết theo!")
        self._say(f"{self._name(pid)} là Thợ Săn. Thợ Săn hãy chọn một người để kéo theo.")
        await self._step(Phase.HUNTER_SHOT, self.settings.hunter_seconds, {pid}, early=True)
        t = self.hunter_target
        if t and self.players[t].alive:
            role = ROLE_VI[self.players[t].role]
            self._event(f"💥 Thợ Săn đã bắn {self._name(t)}.")
            self._say(f"Thợ Săn đã bắn {self._name(t)}."
                      + (f" {self._name(t)} là {role}." if self.settings.reveal_role_on_death else ""))
            self._hist(f"Thợ Săn {self._name(pid)} bắn chết {self._name(t)} ({role}).")
            self._kill(t, "hunter")
        else:
            self._event("Thợ Săn đã hạ súng, không bắn ai.")
            self._say("Thợ Săn đã hạ súng, không bắn ai.")
            self._hist(f"Thợ Săn {self._name(pid)} không bắn ai.")
        self.hunter_shooter = None
        await self._announce(Phase.VERDICT)

    async def _end(self) -> None:
        self.phase, self.deadline, self.actors = Phase.ENDED, None, set()
        won = "Phe Dân Làng" if self.winner == "village" else "Phe Ma Sói"
        self._event(("🏆 " if self.winner == "village" else "🐺 ") + f"{won} chiến thắng!")
        self._say(f"Ván chơi kết thúc. {won} chiến thắng! Sau đây là diễn biến của ván chơi.")
        period = None
        for h in self.history:
            if h["period"] != period:
                period = h["period"]
                self._say(period + ".")
            self._say(h["text"])
        self._say("Cảm ơn mọi người đã chơi.")
        await self._broadcast(self)

    # ------------------------------------------------------------ hành động
    async def handle_action(self, pid: str, action: str, target=None) -> None:
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
            raise GameError("Hết lượt của bạn rồi" if ph.value.startswith("night") else "Chưa đến lượt của bạn")
        if ph in FREEZABLE and pid == self.frozen:
            if action != "frozen_ack":
                raise GameError("Bạn đang bị Sói Băng phong ấn")
            self._done(pid)
            await self._broadcast(self)
            return

        if ph == Phase.NIGHT_CREATOR and action == "creator_pick":
            if target not in [r.value for r in self.creator_options]:
                raise GameError("Hãy chọn một trong hai lá")
            self.creator_choice = Role(target)
            self._done(pid)

        elif ph == Phase.NIGHT_CLONER and action == "clone":
            self._need_alive(target)
            if target == pid:
                raise GameError("Hãy chọn người khác")
            self.clone_target = target
            self._note(pid, f"🧬 Bạn đã chọn nhân bản {self._name(target)}.")
            self._hist(f"Kẻ Nhân Bản {p.name} chọn nhân bản {self._name(target)}.")
            self._done(pid)

        elif ph == Phase.NIGHT_GUARD and action == "guard":
            self._need_alive(target)
            if target == self.last_guarded:
                raise GameError("Không được bảo vệ một người hai đêm liền")
            self.guard_target = target
            self._note(pid, f"🛡️ Đêm {self.day_no}: bạn bảo vệ {self._name(target)}")
            self._hist(f"Bảo Vệ {p.name} bảo vệ {self._name(target)}.")
            self._done(pid)

        elif ph == Phase.NIGHT_WOLVES and action == "sick_ack":
            self._done(pid)
        elif ph == Phase.NIGHT_WOLVES and action == "wolf_vote":
            picks = target if isinstance(target, list) else [target]
            if len(set(map(str, picks))) != self.bites:
                raise GameError(f"Đêm nay bầy được cắn {self.bites} người")
            for t in picks:
                self._need_alive(t)
                if self.is_wolf(self.players[t]):
                    raise GameError("Không thể cắn đồng đội")
            self.wolf_votes[pid] = sorted(picks)  # đổi được đến khi hết giờ

        elif ph == Phase.NIGHT_WITCH and action == "witch_heal":
            if not self.witch_heal_left or target not in self.wolf_targets:
                raise GameError("Không thể dùng bình cứu cho người này")
            self.witch_heal, self.witch_heal_left = target, False
            self._note(pid, f"🧪 Đêm {self.day_no}: bạn cứu {self._name(target)}")
            self._hist(f"Phù Thủy {p.name} dùng bình cứu cho {self._name(target)}.")
        elif ph == Phase.NIGHT_WITCH and action == "witch_poison":
            if not self.witch_poison_left:
                raise GameError("Bạn đã dùng bình độc")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự đầu độc")
            self.witch_poison, self.witch_poison_left = target, False
            self._note(pid, f"☠️ Đêm {self.day_no}: bạn đầu độc {self._name(target)}")
            self._hist(f"Phù Thủy {p.name} đầu độc {self._name(target)}.")
        elif ph == Phase.NIGHT_WITCH and action == "witch_done":
            self._done(pid)

        elif ph == Phase.NIGHT_SEER and action == "seer_check":
            if self.seer_result:
                raise GameError("Bạn đã soi rồi")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự soi mình")
            wolf = self.is_wolf(self.players[target])  # Kẻ Phản Bội, Kẻ Bị Nguyền chưa hoá: phe Dân
            self.seer_result = f"{self._name(target)} thuộc {'PHE SÓI 🐺' if wolf else 'phe Dân Làng'}"
            self._note(pid, f"🔮 Đêm {self.day_no}: {self.seer_result}")
            self._hist(f"Tiên Tri {p.name} soi {self._name(target)}: {'phe Sói' if wolf else 'phe Dân'}.")
        elif ph == Phase.NIGHT_SEER and action == "seer_ack":
            self._done(pid)

        elif ph == Phase.NIGHT_MOON and action in ("moon_check", "moon_shield"):
            if self.moon_result or self.moon_shield:
                raise GameError("Đêm nay bạn đã dùng năng lực")
            self._need_alive(target)
            if action == "moon_check":
                if target == pid:
                    raise GameError("Không thể tự soi mình")
                role = ROLE_VI[self.players[target].role]
                self.moon_result = f"{self._name(target)} là {role}"
                self._note(pid, f"🌙 Đêm {self.day_no}: {self.moon_result}")
                self._hist(f"Nguyệt Nữ {p.name} soi {self._name(target)}: {role}.")
            else:
                self.moon_shield = target
                self._note(pid, f"🌙 Đêm {self.day_no}: bạn che chắn {self._name(target)}")
                self._hist(f"Nguyệt Nữ {p.name} che chắn {self._name(target)}.")
                self._done(pid)
        elif ph == Phase.NIGHT_MOON and action == "moon_ack":
            self._done(pid)

        elif ph == Phase.NIGHT_SNOW and action == "freeze":
            self._need_alive(target)
            if self.is_wolf(self.players[target]):
                raise GameError("Không thể phong ấn đồng đội")
            if target == self.moon_shield:  # lá chắn mặt trăng chặn được phong ấn
                self._hist(f"Sói Băng {p.name} định phong ấn {self._name(target)} nhưng bị lá chắn mặt trăng chặn.")
            else:
                self.frozen_next = target
                self._hist(f"Sói Băng {p.name} phong ấn {self._name(target)} cho đêm sau.")
            self._note(pid, f"❄️ Đêm {self.day_no}: bạn phong ấn {self._name(target)} (có hiệu lực đêm sau)")
            self._done(pid)

        elif ph == Phase.NIGHT_SPELL and action == "silence":
            self._need_alive(target)
            if target == pid:
                raise GameError("Hãy chọn người khác")
            self.spell_target = target
            self._note(pid, f"🤐 Đêm {self.day_no}: bạn phù phép {self._name(target)}")
            self._hist(f"Kẻ Phù Phép {p.name} làm {self._name(target)} bị câm.")
            self._done(pid)

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
            if (ended or o.id == pid or me.narrator
                    or (not o.alive and self.settings.reveal_role_on_death)
                    or (self.is_wolf(me) and self.is_wolf(o))):
                return o.role.value
            return None

        narr = self.narrator
        return {
            "type": "state",
            "code": self.code,
            "phase": self.phase.value,
            "day": self.day_no,
            "deadline": self.deadline,
            "server_now": time.time(),
            "me": {"id": me.id, "name": me.name, "is_host": me.is_host, "alive": me.alive,
                   "narrator": me.narrator, "role": me.role.value if me.role else None,
                   "wolf": self.is_wolf(me) if me.role else False},
            "narrator": {"name": narr.name, "connected": narr.connected} if narr else None,
            "players": [{"id": o.id, "name": o.name, "alive": o.alive,
                         "connected": o.connected, "is_host": o.is_host,
                         "role": visible_role(o)} for o in self.seated()],
            "settings": asdict(self.settings),
            "roles_custom": self.roles_custom,
            "limits": {"min": MIN_PLAYERS, "max": MAX_PLAYERS, "creator_extra": CREATOR_EXTRA},
            "log": self.log[-40:],
            "private": me.private_log[-20:],
            "action": self._action_for(me),
            "votes": self.day_votes if self.phase in (Phase.DAY_VOTE, Phase.VERDICT) else {},
            "hunter": self.hunter_shooter,
            "silenced": self.silenced,
            "speech": self.speech[-150:] if me.narrator else [],
            "history": self.history if ended else [],
            "winner": self.winner,
        }

    def _action_for(self, me: Player) -> dict | None:
        """Việc người này cần làm ngay bây giờ.

        act: hành động gửi lên khi xác nhận; pick: số người cần chọn;
        result/ack: kết quả soi và hành động để đóng lượt; info: chỉ thông báo.
        """
        if me.id not in self.actors:
            return None
        ph = self.phase
        alive = [p.id for p in self.alive()]
        others = [x for x in alive if x != me.id]
        if ph in FREEZABLE and me.id == self.frozen:
            return {"kind": "frozen", "ack": "frozen_ack",
                    "info": "Sói Băng đã phong ấn bạn. Đêm nay bạn không dùng được năng lực."}
        if ph == Phase.NIGHT_CREATOR:
            return {"kind": "creator", "act": "creator_pick",
                    "options": [r.value for r in self.creator_options]}
        if ph == Phase.NIGHT_CLONER:
            return {"kind": "cloner", "act": "clone", "pick": 1, "targets": others}
        if ph == Phase.NIGHT_GUARD:
            return {"kind": "guard", "act": "guard", "pick": 1, "last": self.last_guarded,
                    "targets": [x for x in alive if x != self.last_guarded]}
        if ph == Phase.NIGHT_WOLVES:
            if self.bites == 0:
                return {"kind": "wolf_sick", "ack": "sick_ack",
                        "info": "Đêm qua bầy đã cắn phải Người Bệnh. Cả bầy nhiễm bệnh, đêm nay không cắn được ai."}
            return {"kind": "wolf_vote", "act": "wolf_vote", "pick": self.bites,
                    "chosen": self.wolf_votes.get(me.id) or [],
                    "team_votes": self.wolf_votes,
                    "targets": [p.id for p in self.alive() if not self.is_wolf(p)]}
        if ph == Phase.NIGHT_WITCH:
            return {"kind": "witch", "act": "witch_poison", "pick": 1,
                    "victims": self.wolf_targets if (self.witch_heal_left or self.witch_heal) else [],
                    "heal_left": self.witch_heal_left, "healed": self.witch_heal,
                    "poison_left": self.witch_poison_left, "poisoned": self.witch_poison,
                    "targets": others}
        if ph == Phase.NIGHT_SEER:
            return {"kind": "seer", "act": "seer_check", "pick": 1,
                    "result": self.seer_result, "ack": "seer_ack", "targets": others}
        if ph == Phase.NIGHT_MOON:
            return {"kind": "moon", "pick": 1, "targets": alive,
                    "result": self.moon_result, "ack": "moon_ack"}
        if ph == Phase.NIGHT_SNOW:
            return {"kind": "snow", "act": "freeze", "pick": 1,
                    "targets": [p.id for p in self.alive() if not self.is_wolf(p)]}
        if ph == Phase.NIGHT_SPELL:
            return {"kind": "spell", "act": "silence", "pick": 1, "targets": others}
        if ph == Phase.DAY_VOTE:
            return {"kind": "vote", "act": "vote", "pick": 1,
                    "chosen": self.day_votes.get(me.id), "targets": others}
        if ph == Phase.HUNTER_SHOT:
            return {"kind": "hunter", "act": "hunter_shoot", "pick": 1, "targets": others}
        return None
