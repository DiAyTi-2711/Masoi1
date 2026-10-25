"""Chạy thử hàng loạt ván với bot để kiểm tra engine: python simulate.py"""
import asyncio, random, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from game import GameError, Phase, Role, Room


async def bot(room: Room, pid: str):
    await asyncio.sleep(random.uniform(0.001, 0.01))
    a = room.view_for(pid)["action"]
    if not a:
        return
    k, ts, n = a["kind"], a.get("targets") or [], a.get("pick", 1)
    act = lambda *x: room.handle_action(pid, *x)
    try:
        if a.get("info") or a.get("result"):
            return await act(a["ack"])
        if k == "creator":
            return await act("creator_pick", random.choice(a["options"]))
        if k == "moon":
            t = random.choice(ts)
            return await act("moon_shield" if random.random() < .5 or t == pid else "moon_check", t)
        if k == "witch":
            if a["heal_left"] and a["victims"] and random.random() < .5:
                await act("witch_heal", random.choice(a["victims"]))
            if a["poison_left"] and ts and random.random() < .3:
                await act("witch_poison", random.choice(ts))
            return await act("witch_done")
        if k == "wolf_vote":  # bám theo lựa chọn của đồng đội
            team = list(a["team_votes"].values())
            choice = team[0] if team else random.sample(ts, min(n, len(ts)))
            return await act("wolf_vote", choice)
        if k == "vote" and random.random() < .15:
            return await act("vote", "skip")
        if ts:
            await act(a["act"], random.choice(ts))
        elif k == "hunter":
            await act("hunter_skip")
    except GameError:
        pass


async def play(n: int, roles=None) -> tuple[str, int, list]:
    async def broadcast(room):
        for pid in list(room.actors):
            asyncio.create_task(bot(room, pid))
    room = Room("TEST", broadcast)
    room.announce_seconds, room.call_seconds = 0.001, 0.001
    room.add_player("Quản trò", is_host=True, narrator=True)
    for i in range(n):
        room.add_player(f"P{i}")
    if roles:
        room.update_settings({"roles": roles})
    s = room.settings
    s.creator_seconds = s.wolves_seconds = s.role_seconds = 0.05
    s.vote_seconds = s.hunter_seconds = 0.3
    s.discuss_seconds = 0.01
    await room.start()
    await asyncio.wait_for(room.task, 120)
    assert room.phase == Phase.ENDED and room.winner, room.log[-3:]
    assert room.speech and room.history and not room.narrator.role
    if room.winner == "village":
        assert not room.wolves()
    return room.winner, room.day_no, [e["text"] for e in room.log]


async def main():
    every = {r.value: 1 for r in Role if r not in (Role.WEREWOLF, Role.VILLAGER)}
    results, seen = [], Counter()
    for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 30):
        n = random.randint(10, 30)
        # một nửa số ván bật TẤT CẢ vai để thử mọi tương tác
        roles = {**every, "werewolf": max(1, n // 4 - 3)} if i % 2 and n >= 16 else None
        w, d, log = await play(n, roles)
        results.append((w, d))
        for t in log:
            for key in ("Thợ Săn đã bắn", "bị phù phép", "bình cứu được hoàn", "bình độc được hoàn"):
                if key in t:
                    seen[key] += 1
    print(f"{len(results)} ván OK (có máy quản trò) – {dict(Counter(w for w, _ in results))}, "
          f"trung bình {sum(d for _, d in results) / len(results):.1f} đêm")
    print("Sự kiện đã xảy ra:", dict(seen))

if __name__ == "__main__":
    asyncio.run(main())
