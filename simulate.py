"""Chạy thử hàng loạt ván với bot để kiểm tra engine: python simulate.py"""
import asyncio, random, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from game import GameError, Phase, Role, Room


async def bot(room: Room, pid: str):
    await asyncio.sleep(random.uniform(0.001, 0.01))
    a = room.view_for(pid)["action"]
    if not a:
        return
    k, ts = a["kind"], a.get("targets") or []
    t = random.choice(ts) if ts else None
    try:
        if k == "guard" and t:
            await room.handle_action(pid, "guard", t)
        elif k == "wolf_vote" and t:  # bám theo con mồi đồng đội đã chọn
            team = list(a["team_votes"].values())
            await room.handle_action(pid, "wolf_vote", team[0] if team else t)
        elif k == "seer_check":
            await room.handle_action(pid, "seer_ack" if a["result"] else "seer_check", None if a["result"] else t)
        elif k == "witch":
            if a["heal_left"] and a["victim"] and random.random() < .5:
                await room.handle_action(pid, "witch_heal")
            if a["poison_left"] and t and random.random() < .3:
                await room.handle_action(pid, "witch_poison", t)
            await room.handle_action(pid, "witch_done")
        elif k == "vote":
            await room.handle_action(pid, "vote", t if random.random() < .85 else "skip")
        elif k == "hunter":
            await room.handle_action(pid, "hunter_shoot" if t else "hunter_skip", t)
    except GameError:
        pass


async def play(n: int) -> tuple[str, int]:
    async def broadcast(room):
        for pid in list(room.actors):
            asyncio.create_task(bot(room, pid))
    room = Room("TEST", broadcast)
    room.announce_seconds, room.fake_wait = 0.001, (0.001, 0.002)
    for i in range(n):
        room.add_player(f"P{i}", is_host=i == 0)
    s = room.settings
    s.night_step_seconds = s.vote_seconds = s.hunter_seconds = 0.3
    s.discuss_seconds = 0.01
    await room.start()
    await asyncio.wait_for(room.task, 60)
    assert room.phase == Phase.ENDED and room.winner in ("village", "wolves")
    wolves = len(room.alive(Role.WEREWOLF))
    assert (wolves == 0) == (room.winner == "village")
    return room.winner, room.day_no


async def main():
    results = [await play(random.randint(10, 30)) for _ in range(40)]
    wins = sum(w == "village" for w, _ in results)
    print(f"40 ván OK – Dân thắng {wins}, Sói thắng {40 - wins}, "
          f"trung bình {sum(d for _, d in results) / 40:.1f} đêm")

asyncio.run(main())
