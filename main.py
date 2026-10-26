"""Ma Sói – FastAPI + WebSocket.

Chạy:  python main.py            (in ra địa chỉ LAN + mã QR ngay trong terminal)
  hoặc uvicorn main:app --host 0.0.0.0 --port 8000
Máy chủ có thể là một điện thoại Android (Termux) – xem README.

Lưu ý: trạng thái phòng nằm trong RAM của MỘT tiến trình -> chạy 1 worker.
Muốn nhiều worker/nhiều máy, xem README (Redis pub/sub + sticky session).
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import secrets
import socket
import struct
import time
from functools import lru_cache
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from game import GameError, Phase, Room

app = FastAPI(title="Ma Sói – Quản trò tự động")

rooms: dict[str, Room] = {}
connections: dict[str, dict[str, WebSocket]] = {}  # code -> {player_id: ws}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # bỏ I, O cho dễ đọc
ROOM_TTL = 12 * 3600
PORT = int(os.environ.get("MASOI_PORT", "8000"))
PUBLIC_URL = os.environ.get("MASOI_URL", "").rstrip("/")  # ép địa chỉ nếu tự dò sai


# ---------------------------------------------------------------- địa chỉ LAN & QR
def _interfaces() -> list[tuple[str, str]]:
    """Liệt kê (tên card mạng, IPv4) bằng ioctl SIOCGIFCONF.

    Không cần internet và không dùng netlink (bị Android 11+ chặn), nên chạy được
    trong Termux khi điện thoại đang phát hotspot mà không bật dữ liệu di động.
    """
    found: list[tuple[str, str]] = []
    try:
        import array
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        size = 40 if struct.calcsize("P") == 8 else 32
        buf = array.array("B", b"\0" * size * 64)
        req = struct.pack("iL", len(buf), buf.buffer_info()[0])
        n = struct.unpack("iL", fcntl.ioctl(s.fileno(), 0x8912, req))[0]
        data = buf.tobytes()
        for i in range(0, n, size):
            name = data[i:i + 16].split(b"\0")[0].decode(errors="ignore")
            found.append((name, socket.inet_ntoa(data[i + 20:i + 24])))
        s.close()
    except Exception:
        pass
    # Dự phòng: hỏi hệ điều hành sẽ đi ra mạng bằng IP nào (không gửi gói tin thật)
    for probe in ("8.8.8.8", "192.168.0.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 80))
                found.append(("route", s.getsockname()[0]))
        except OSError:
            pass
    return found


def lan_ips() -> list[str]:
    """IP mà điện thoại khác có thể truy cập, ưu tiên Wi-Fi/hotspot, bỏ 4G."""
    def score(item: tuple[str, str]) -> tuple[int, int]:
        name, ip = item
        cellular = name.startswith(("rmnet", "ccmni", "pdp", "wwan", "v4-rmnet"))
        wifi = name.startswith(("wlan", "ap", "swlan", "softap", "eth", "en", "route"))
        private = 0 if ip.startswith("192.168.") else 1 if re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip) else 2
        return (2 if cellular else 0 if wifi else 1, private)

    seen, out = set(), []
    for name, ip in sorted(_interfaces(), key=score):
        if ip.startswith(("127.", "0.", "169.254.")) or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def join_bases(request: Request) -> list[str]:
    """Địa chỉ gốc để đưa vào QR. Nếu Host đang mở bằng localhost thì thay bằng IP LAN."""
    if PUBLIC_URL:
        return [PUBLIC_URL]
    host = (request.url.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return [f"{request.url.scheme}://{request.headers.get('host')}"]
    port = request.url.port or PORT
    return [f"http://{ip}:{port}" for ip in lan_ips()] or [str(request.base_url).rstrip("/")]


@lru_cache(maxsize=256)
def qr_svg(text: str) -> str:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    svg = img.to_string(encoding="unicode")
    return re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)  # để CSS tự co giãn


async def broadcast(room: Room) -> None:
    """Gửi cho MỖI người một view riêng – vai trò bí mật không bao giờ đi chung."""
    conns = connections.get(room.code, {})

    async def send(pid: str, ws: WebSocket) -> None:
        try:
            await ws.send_json(room.view_for(pid))
        except Exception:
            pass  # socket chết sẽ được dọn ở vòng nhận

    await asyncio.gather(*(send(pid, ws) for pid, ws in list(conns.items())
                           if pid in room.players))


def _purge_old_rooms() -> None:
    now = time.time()
    for code, room in list(rooms.items()):
        if now - room.created_at > ROOM_TTL and not connections.get(code):
            if room.task:
                room.task.cancel()
            rooms.pop(code, None)
            connections.pop(code, None)


def _new_code() -> str:
    while True:
        code = "".join(random.choices(CODE_ALPHABET, k=4))
        if code not in rooms:
            return code


def _get_room(code: str) -> Room:
    room = rooms.get(code.upper())
    if not room:
        raise HTTPException(404, "Không tìm thấy phòng – kiểm tra lại mã")
    return room


class NameReq(BaseModel):
    name: str = ""
    narrator: bool = False  # máy này làm quản trò, không chơi


@app.post("/api/rooms")
async def create_room(req: NameReq):
    _purge_old_rooms()
    room = Room(_new_code(), broadcast)
    try:
        host = room.add_player(req.name, is_host=True, narrator=req.narrator)
    except GameError as e:
        raise HTTPException(400, str(e))
    rooms[room.code] = room
    return {"code": room.code, "player_id": host.id, "token": host.token}


@app.post("/api/rooms/{code}/join")
async def join_room(code: str, req: NameReq):
    room = _get_room(code)
    try:
        p = room.add_player(req.name)
    except GameError as e:
        raise HTTPException(400, str(e))
    await broadcast(room)
    return {"code": room.code, "player_id": p.id, "token": p.token}


async def dispatch(room: Room, pid: str, ws: WebSocket, msg: dict) -> None:
    p = room.players.get(pid)
    if p is None:
        raise GameError("Bạn không còn trong phòng")
    kind = msg.get("type")

    if kind == "action":
        await room.handle_action(pid, str(msg.get("action")), msg.get("target"))
        return
    if kind == "ping":
        await ws.send_json({"type": "pong"})
        return
    if kind == "ready_next":
        room.toggle_next_ready(pid)
        await broadcast(room)
        return
    if kind == "leave":
        room.remove_player(pid)
        await ws.close()
        if not room.players:
            rooms.pop(room.code, None)
        await broadcast(room)
        return

    if not p.is_host:
        raise GameError("Chỉ chủ phòng mới làm được việc này")
    if kind == "settings":
        room.update_settings(msg.get("settings") or {})
    elif kind == "start":
        await room.start()
    elif kind == "bots":
        op = msg.get("op")
        if op == "add":
            room.add_bot()
        elif op == "remove":
            room.remove_bot()
        elif op == "fill":
            room.fill_bots(int(msg.get("n") or 0))
        else:
            raise GameError("Lệnh máy chơi không hợp lệ")
    elif kind == "restart":
        room.reset_to_lobby()
    elif kind == "reset_scores":
        room.reset_scores()
    elif kind == "cancel_game":
        room.cancel_game()
    elif kind == "new_game":
        await room.new_game()
    elif kind == "kick":
        room.remove_player(str(msg.get("target")))
        old = connections.get(room.code, {}).pop(str(msg.get("target")), None)
        if old:
            await old.close(code=4403)
    else:
        raise GameError("Lệnh không hợp lệ")
    await broadcast(room)


@app.websocket("/ws/{code}")
async def ws_endpoint(ws: WebSocket, code: str, pid: str, token: str):
    room = rooms.get(code.upper())
    player = room.players.get(pid) if room else None
    if not player or not secrets.compare_digest(player.token, token):
        await ws.close(code=4401)  # client sẽ xoá phiên và về màn hình chính
        return
    await ws.accept()

    conns = connections.setdefault(room.code, {})
    old = conns.get(pid)
    conns[pid] = ws  # cùng người mở tab mới / mất mạng rồi vào lại
    if old is not None:
        try:
            await old.close(code=4409)
        except Exception:
            pass
    player.connected = True
    await broadcast(room)

    try:
        while True:
            msg = await ws.receive_json()
            try:
                await dispatch(room, pid, ws, msg if isinstance(msg, dict) else {})
            except GameError as e:
                await ws.send_json({"type": "error", "message": str(e)})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if conns.get(pid) is ws:
            conns.pop(pid, None)
            if pid in room.players:
                room.players[pid].connected = False
            await broadcast(room)


@app.get("/api/rooms/{code}/invite")
async def invite(code: str, request: Request, i: int = 0):
    """Link + mã QR để người khác quét vào thẳng phòng. `i` để đổi sang IP khác."""
    room = _get_room(code)
    bases = join_bases(request)
    idx = i % len(bases)
    url = f"{bases[idx]}/?room={room.code}"
    return {"url": url, "svg": qr_svg(url), "index": idx, "count": len(bases)}


@app.get("/api/health")
async def health():
    return {"rooms": len(rooms),
            "playing": sum(r.phase not in (Phase.LOBBY, Phase.ENDED) for r in rooms.values())}


# Giao diện: một file index.html duy nhất, nằm cạnh main.py (hoặc trong frontend/)
HERE = Path(__file__).parent
INDEX = next(p for p in (HERE / "index.html", HERE.parent / "frontend" / "index.html") if p.exists())


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX, headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    import uvicorn

    urls = [PUBLIC_URL] if PUBLIC_URL else [f"http://{ip}:{PORT}" for ip in lan_ips()]
    print("\n🐺 Ma Sói đang chạy. Trên máy này mở: http://localhost:%d" % PORT)
    for u in urls:
        print(f"   Máy khác trong cùng Wi-Fi/hotspot: {u}")
    if urls:
        q = qrcode.QRCode(border=1)
        q.add_data(urls[0])
        q.print_ascii(invert=True)
    else:
        print("   ⚠️ Chưa thấy mạng LAN – hãy bật Wi-Fi hoặc điểm phát sóng.")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
