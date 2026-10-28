"""SQLite 数据模型与初始化 — zilearn（儿童识字，对标洪恩识字）

设计详见 docs/PRD.md §7。核心表：users / hanzi / zi_content / user_hanzi /
quiz_log / stars / weekly_reports。绘本表（books）P4 接入时启用。
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "zilearn.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  grade         TEXT,                             -- '1A'..'6B'（引导流选择）
  target_chars  INTEGER,                          -- 计划：目标字量
  plan_days     INTEGER,                          -- 计划：完成天数
  plan_start    TEXT,                             -- 计划：起始日（日历日）
  onboarded     INTEGER DEFAULT 0,                -- 1=引导流完成（年级+计划+摸底）
  created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hanzi (
  id             INTEGER PRIMARY KEY,
  char           TEXT NOT NULL UNIQUE,
  pinyin         TEXT NOT NULL,                   -- 课内主音（tone 符号，如 tiān）
  tone3          TEXT NOT NULL,                   -- tian1（音频文件名后缀）
  alt_readings   TEXT,                            -- 第二读音 JSON [["zhǎng","zhang3","长大"]]
  unit           TEXT NOT NULL,                   -- 识字1 | 课文3 | 拼音单元 | 语文园地
  seq            INTEGER NOT NULL,                -- 教材出现顺序（学习顺序）
  book           TEXT NOT NULL DEFAULT '1a'
);

CREATE TABLE IF NOT EXISTS zi_content (
  hanzi_id  INTEGER PRIMARY KEY REFERENCES hanzi(id),
  words     TEXT,          -- 组词 JSON ["天上","白天"]（3-6 个）
  sentence  TEXT,          -- 首句（= sentences[0]，兼容单句引用处）
  sentences TEXT,          -- 例句 JSON ["天上飘着白云。","白天太阳出来了。"]（2-3 句）
  etym_type TEXT,          -- picto|component|mnemonic|scene
  etym      TEXT,          -- 字源/部件故事
  emoji     TEXT           -- 视觉锚点（高置信才配，可空）
);

CREATE TABLE IF NOT EXISTS user_hanzi (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  hanzi_id    INTEGER NOT NULL REFERENCES hanzi(id),
  status      TEXT NOT NULL DEFAULT 'new',   -- new|learning|mastered|skipped
  stage       INTEGER DEFAULT 0,             -- 已完成复习次数（0=首学当天）
  next_date   TEXT,                          -- 下次复习日（mastered 后为 NULL）
  first_date  TEXT,                          -- 首学日
  last_date   TEXT,
  wrong_count INTEGER DEFAULT 0,
  audit_date  TEXT,                          -- 最近抽检日
  UNIQUE(user_id, hanzi_id)
);

CREATE TABLE IF NOT EXISTS quiz_log (
  id        INTEGER PRIMARY KEY,
  user_id   INTEGER NOT NULL,
  hanzi_id  INTEGER NOT NULL,
  phase     TEXT NOT NULL,               -- learn|review|weekly|verify|audit
  quiz_type TEXT,                        -- read|listen|mean
  correct   INTEGER NOT NULL,
  ts        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qlog_user ON quiz_log(user_id, hanzi_id, phase);

CREATE TABLE IF NOT EXISTS stars (
  id      INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  date    TEXT NOT NULL,
  src     TEXT NOT NULL,                 -- weekly|book
  n       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_reports (
  user_id   INTEGER NOT NULL,
  week_no   INTEGER NOT NULL,
  score     INTEGER NOT NULL,
  total     INTEGER NOT NULL,
  wrong_ids TEXT,                        -- 错字 id JSON
  done      INTEGER DEFAULT 0,           -- 1=已完成结算
  UNIQUE(user_id, week_no)
);

CREATE TABLE IF NOT EXISTS confirm_pick (   -- 摸底：速选认识的字（待验证）
  user_id   INTEGER NOT NULL,
  hanzi_id  INTEGER NOT NULL,
  result    TEXT,                        -- NULL=待验证 | pass | fail
  UNIQUE(user_id, hanzi_id)
);

CREATE TABLE IF NOT EXISTS follow_log (   -- 跟读开口记录（有效性度量：开口率/超时率/静默降级率）
  id        INTEGER PRIMARY KEY,
  user_id   INTEGER NOT NULL,
  hanzi_id  INTEGER,                      -- readcard 行取自今日新字，均可回查
  char      TEXT NOT NULL,
  kind      TEXT NOT NULL,                -- char|word|sent|read
  heard     INTEGER,                      -- 1=读到 0=超时 NULL=无麦降级（不夸不卡）
  tries     INTEGER DEFAULT 0,            -- 重播范读轮数（0=一次过）
  peak_rms  REAL,                         -- 检测窗峰值 RMS（v19 遥测：判"没读"vs"漏检"，不存音频只存标量）
  noise_base REAL,                        -- 结束时滚动本底
  thr_used  REAL,                         -- 结束时实发阈值
  voiced_ms INTEGER,                      -- 累积超阈语音毫秒
  dur_ms    INTEGER,                      -- 检测窗实际时长
  ts_ms     INTEGER,                      -- 客户端时刻（区分弱网批量交付与服务端 ts）
  ua        TEXT,                         -- 截断 UA（设备/浏览器/刷新率排查）
  ts        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_flog_user ON follow_log(user_id, ts);

CREATE TABLE IF NOT EXISTS questions (  -- 预生成题库（离线生成+人工审查后才可出题）
  id        INTEGER PRIMARY KEY,
  hanzi_id  INTEGER NOT NULL REFERENCES hanzi(id),
  quiz_type TEXT NOT NULL,               -- listen|mean|sent|find
  variant   INTEGER NOT NULL DEFAULT 1,  -- 同字同题型变体号
  payload   TEXT NOT NULL,               -- 题面 JSON（结构同运行时出题）
  checks    TEXT,                        -- 生成期校验记录 JSON（干扰项代入结果等）
  verified  INTEGER DEFAULT 0,           -- 0=待审 1=通过 -1=打回
  verdict   TEXT,                        -- 审查备注 / 打回原因
  UNIQUE(hanzi_id, quiz_type, variant)
);
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # 迁移：旧库补 sentences 列（新库 SCHEMA 已含）
    try:
        db.execute("ALTER TABLE zi_content ADD COLUMN sentences TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：旧库补 follow_log 遥测列（v19，新库 SCHEMA 已含）
    for col, decl in [("peak_rms", "REAL"), ("noise_base", "REAL"), ("thr_used", "REAL"),
                      ("voiced_ms", "INTEGER"), ("dur_ms", "INTEGER"), ("ts_ms", "INTEGER"), ("ua", "TEXT")]:
        try:
            db.execute(f"ALTER TABLE follow_log ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    db.commit()
    db.close()


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db
