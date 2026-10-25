-- Ma Sói – lược đồ PostgreSQL để LƯU LỊCH SỬ & thống kê.
-- Trạng thái ván đang chơi vẫn nằm trong RAM/Redis (cần độ trễ thấp);
-- DB chỉ ghi khi có sự kiện quan trọng (bắt đầu, mỗi hành động, kết thúc).

CREATE TYPE role_t  AS ENUM ('werewolf','villager','seer','bodyguard','witch','hunter');
CREATE TYPE phase_t AS ENUM ('lobby','night_guard','night_wolves','night_seer','night_witch',
                             'dawn','day_discuss','day_vote','verdict','hunter_shot','ended');

CREATE TABLE users (                -- tuỳ chọn: chỉ cần nếu muốn đăng nhập / xếp hạng
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE rooms (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code        CHAR(4) NOT NULL,                 -- mã 4 chữ, tái sử dụng sau khi phòng đóng
  host_user   UUID REFERENCES users(id),
  settings    JSONB NOT NULL,                   -- {"roles":{"werewolf":3,...},"night_step_seconds":30,...}
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX rooms_open_code ON rooms(code) WHERE closed_at IS NULL;

CREATE TABLE games (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  room_id     UUID NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  settings    JSONB NOT NULL,                   -- ảnh chụp cấu hình lúc bắt đầu
  winner      TEXT CHECK (winner IN ('village','wolves')),
  nights      SMALLINT NOT NULL DEFAULT 0,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at    TIMESTAMPTZ
);

CREATE TABLE game_players (
  game_id     UUID NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  seat        SMALLINT NOT NULL,                -- 0..29
  user_id     UUID REFERENCES users(id),
  name        TEXT NOT NULL,
  role        role_t NOT NULL,
  died_night  SMALLINT,                         -- NULL = sống sót
  death_cause TEXT CHECK (death_cause IN ('wolf','poison','lynch','hunter')),
  PRIMARY KEY (game_id, seat)
);

-- Nhật ký hành động: đủ để "phát lại" cả ván hoặc điều tra khiếu nại.
CREATE TABLE game_actions (
  id          BIGSERIAL PRIMARY KEY,
  game_id     UUID NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  day_no      SMALLINT NOT NULL,
  phase       phase_t NOT NULL,
  actor_seat  SMALLINT,                         -- NULL = hệ thống
  action      TEXT NOT NULL,                    -- guard, wolf_vote, seer_check, witch_heal, vote...
  target_seat SMALLINT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX game_actions_game ON game_actions(game_id, id);
