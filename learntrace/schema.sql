-- LearnTrace MVP schema
-- 三個資料庫檔：
--   main  (research.db) 課程內容、研究事件、教師資料
--   ident (ident.db)    帳號與研究代號對照（身分區）
--   ops   (ops.db)      系統營運資料（所有學生的學習進度，不匯出）
-- 時間一律由資料庫產生（伺服器時間），不接受前端時間。

-- ── 身分區 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ident.accounts (
  account_id   TEXT PRIMARY KEY,
  login_id     TEXT NOT NULL UNIQUE,      -- 正式版加密儲存；MVP 只有假資料
  role         TEXT NOT NULL CHECK (role IN ('student','teacher','researcher')),
  display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ident.research_codes (
  account_id    TEXT NOT NULL,
  course_id     TEXT NOT NULL,
  research_code TEXT NOT NULL UNIQUE CHECK (research_code GLOB 'S-[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'),
  consent       TEXT NOT NULL DEFAULT 'pending'
                CHECK (consent IN ('pending','consented','declined','withdrawn')),
  consent_at    TEXT,
  withdrawn_at  TEXT,
  PRIMARY KEY (account_id, course_id)
);

-- ── 系統營運資料（所有學生；不匯出、不進洞察卡）────────────
CREATE TABLE IF NOT EXISTS ops.app_progress (
  account_id           TEXT NOT NULL,
  round_id             TEXT NOT NULL,
  concept_id           TEXT NOT NULL,
  level1_viewed_at     TEXT,
  practice_answered_at TEXT,
  current_level        INTEGER NOT NULL DEFAULT 1 CHECK (current_level BETWEEN 1 AND 3),
  PRIMARY KEY (account_id, round_id, concept_id)
);

CREATE TABLE IF NOT EXISTS ops.app_unit (
  account_id        TEXT NOT NULL,
  round_id          TEXT NOT NULL,
  unit_completed_at TEXT,
  quiz_completed_at TEXT,
  PRIMARY KEY (account_id, round_id)
);

-- ── 課程內容與版本 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS courses (
  course_id TEXT PRIMARY KEY,
  name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_lists (
  list_id     TEXT PRIMARY KEY,
  course_id   TEXT NOT NULL REFERENCES courses(course_id),
  version     INTEGER NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  locked_at   TEXT
);

CREATE TABLE IF NOT EXISTS concepts (
  concept_id TEXT PRIMARY KEY,
  list_id    TEXT NOT NULL REFERENCES concept_lists(list_id),
  seq        INTEGER NOT NULL,
  name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
  round_id           TEXT PRIMARY KEY,
  course_id          TEXT NOT NULL REFERENCES courses(course_id),
  round_no           INTEGER NOT NULL,
  unit_id            TEXT NOT NULL,
  unit_name          TEXT NOT NULL,
  content_version    INTEGER NOT NULL,
  concept_list_id    TEXT NOT NULL REFERENCES concept_lists(list_id),
  stage              TEXT NOT NULL DEFAULT 'prediction'
                     CHECK (stage IN ('prediction','learning','quiz','quiz_confirmed','card_open','closed')),
  learning_opened_at TEXT,
  quiz_opened_at     TEXT,
  quiz_confirmed_at  TEXT,
  card_opened_at     TEXT
);

CREATE TABLE IF NOT EXISTS unit_content (
  content_id TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL REFERENCES concepts(concept_id),
  level      INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  example    TEXT,
  version    INTEGER NOT NULL,
  locked_at  TEXT,
  UNIQUE (concept_id, level, version)
);

CREATE TABLE IF NOT EXISTS practice_items (
  item_id     TEXT PRIMARY KEY,
  concept_id  TEXT NOT NULL REFERENCES concepts(concept_id),
  stem        TEXT NOT NULL,
  options     TEXT NOT NULL,              -- JSON 陣列
  answer      TEXT NOT NULL,              -- 不傳到前端
  hint        TEXT NOT NULL,
  explanation TEXT NOT NULL,
  version     INTEGER NOT NULL,
  locked_at   TEXT
);

CREATE TABLE IF NOT EXISTS quiz_items (
  quiz_item_id      TEXT PRIMARY KEY,
  round_id          TEXT NOT NULL REFERENCES rounds(round_id),
  concept_id        TEXT NOT NULL REFERENCES concepts(concept_id),
  seq               INTEGER NOT NULL,
  stem              TEXT NOT NULL,
  options           TEXT NOT NULL,        -- JSON 陣列
  answer            TEXT NOT NULL,        -- 不傳到學生端
  author_teacher_id TEXT NOT NULL,
  version           INTEGER NOT NULL,
  locked_at         TEXT,
  UNIQUE (round_id, concept_id, seq)
);

-- ── 研究事件（只寫入同意研究的學生）──────────────────────
CREATE TABLE IF NOT EXISTS event_log (
  log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id        TEXT NOT NULL REFERENCES rounds(round_id),
  research_code   TEXT NOT NULL,
  concept_id      TEXT,
  event_type      TEXT NOT NULL CHECK (event_type IN
                  ('content_viewed','practice_answer','level_down','not_understood',
                   'question_submitted','unit_completed')),
  payload         TEXT NOT NULL DEFAULT '{}',
  server_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  content_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
  round_id      TEXT NOT NULL,
  research_code TEXT NOT NULL,
  concept_id    TEXT NOT NULL,
  signal_type   TEXT NOT NULL CHECK (signal_type IN
                ('practice_first_wrong','level_down','not_understood','question_submitted')),
  first_at      TEXT NOT NULL,
  source_log_id INTEGER NOT NULL REFERENCES event_log(log_id),
  PRIMARY KEY (round_id, research_code, concept_id, signal_type)   -- 去重：每種訊號最多 1 次
);

CREATE TABLE IF NOT EXISTS questions (
  question_id   TEXT PRIMARY KEY,
  round_id      TEXT NOT NULL,
  research_code TEXT NOT NULL,
  concept_id    TEXT NOT NULL,
  text_raw      TEXT NOT NULL,            -- 正式版加密、僅研究者可讀
  text_redacted TEXT NOT NULL,            -- 只有這一欄可以送外部 AI
  pii_flag      INTEGER NOT NULL CHECK (pii_flag IN (0,1)),
  server_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS question_coding (
  question_id TEXT NOT NULL REFERENCES questions(question_id),
  coder       TEXT NOT NULL CHECK (coder IN ('A','B')),
  concept_id  TEXT NOT NULL,
  coded_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (question_id, coder)
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
  round_id      TEXT NOT NULL,
  research_code TEXT NOT NULL,
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  submitted_at  TEXT,
  status        TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started','submitted','expired')),
  PRIMARY KEY (round_id, research_code)
);

CREATE TABLE IF NOT EXISTS quiz_responses (
  round_id      TEXT NOT NULL,
  research_code TEXT NOT NULL,
  quiz_item_id  TEXT NOT NULL REFERENCES quiz_items(quiz_item_id),
  choice        TEXT,                     -- NULL = 未作答
  correct       INTEGER CHECK (correct IN (0,1)),
  submitted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (round_id, research_code, quiz_item_id)
);

-- ── 教師資料 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS teacher_predictions (
  prediction_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id             TEXT NOT NULL REFERENCES rounds(round_id),
  teacher_id           TEXT NOT NULL,
  rank                 INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 3),
  concept_id           TEXT NOT NULL REFERENCES concepts(concept_id),
  reason               TEXT,
  submitted_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  prediction_version   INTEGER NOT NULL,
  concept_list_version INTEGER NOT NULL,
  UNIQUE (round_id, rank),
  UNIQUE (round_id, concept_id)
);

CREATE TABLE IF NOT EXISTS quiz_confirmations (
  round_id             TEXT PRIMARY KEY REFERENCES rounds(round_id),
  teacher_id           TEXT NOT NULL,
  map_ok               INTEGER NOT NULL CHECK (map_ok = 1),
  closed_ok            INTEGER NOT NULL CHECK (closed_ok = 1),
  confirmed_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  quiz_data_version    INTEGER NOT NULL,
  concept_list_version INTEGER NOT NULL,
  consented_n          INTEGER NOT NULL,
  completed_n          INTEGER NOT NULL,
  not_completed_n      INTEGER NOT NULL,
  missing_cells        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS insight_cards (
  card_id           TEXT PRIMARY KEY,
  round_id          TEXT NOT NULL REFERENCES rounds(round_id),
  concept_id        TEXT NOT NULL REFERENCES concepts(concept_id),
  analyzable_n      INTEGER NOT NULL,
  consented_n       INTEGER NOT NULL,
  counts            TEXT NOT NULL,        -- 精確人數（研究者用）
  display           TEXT NOT NULL,        -- 教師看到的版本（少於 5 人已遮蔽）
  askers_n          INTEGER NOT NULL,
  confusion_summary TEXT,                 -- 少於 5 位提問者時為 NULL
  ai_suggestion     TEXT NOT NULL,
  ai_model          TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  generated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (round_id, concept_id)
);

CREATE TABLE IF NOT EXISTS card_responses (
  card_id      TEXT PRIMARY KEY REFERENCES insight_cards(card_id),
  teacher_id   TEXT NOT NULL,
  response     TEXT NOT NULL CHECK (response IN ('adopt','later','known','disagree')),
  note         TEXT NOT NULL CHECK (length(trim(note)) > 0),
  submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS teacher_action_notes (
  note_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id   TEXT NOT NULL REFERENCES insight_cards(card_id),
  note      TEXT NOT NULL CHECK (length(trim(note)) > 0),
  server_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── 鎖定：資料庫端拒絕修改 ─────────────────────────────
-- 輪次階段只能往前一步
CREATE TRIGGER IF NOT EXISTS rounds_stage_forward
BEFORE UPDATE OF stage ON rounds
WHEN (CASE NEW.stage WHEN 'prediction' THEN 0 WHEN 'learning' THEN 1 WHEN 'quiz' THEN 2
        WHEN 'quiz_confirmed' THEN 3 WHEN 'card_open' THEN 4 ELSE 5 END)
  != (CASE OLD.stage WHEN 'prediction' THEN 0 WHEN 'learning' THEN 1 WHEN 'quiz' THEN 2
        WHEN 'quiz_confirmed' THEN 3 WHEN 'card_open' THEN 4 ELSE 5 END) + 1
BEGIN SELECT RAISE(ABORT, 'locked: stage can only move forward one step'); END;

CREATE TRIGGER IF NOT EXISTS rounds_no_delete BEFORE DELETE ON rounds
BEGIN SELECT RAISE(ABORT, 'locked: rounds cannot be deleted'); END;

-- 驗證小考作答狀態：只能從 started 變成 submitted 或 expired
CREATE TRIGGER IF NOT EXISTS quiz_attempts_final
BEFORE UPDATE ON quiz_attempts
WHEN OLD.status <> 'started' OR NEW.status NOT IN ('submitted','expired')
  OR NEW.research_code <> OLD.research_code OR NEW.round_id <> OLD.round_id
BEGIN SELECT RAISE(ABORT, 'locked: quiz attempt already final'); END;

CREATE TRIGGER IF NOT EXISTS quiz_attempts_no_delete BEFORE DELETE ON quiz_attempts
BEGIN SELECT RAISE(ABORT, 'locked: quiz_attempts is append-only'); END;

-- 已鎖定的內容不能改、不能刪
CREATE TRIGGER IF NOT EXISTS concept_lists_locked BEFORE UPDATE ON concept_lists
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: concept list is locked'); END;

CREATE TRIGGER IF NOT EXISTS concepts_locked BEFORE UPDATE ON concepts
WHEN (SELECT locked_at FROM concept_lists WHERE list_id = OLD.list_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: concept list is locked'); END;

CREATE TRIGGER IF NOT EXISTS concepts_no_delete BEFORE DELETE ON concepts
WHEN (SELECT locked_at FROM concept_lists WHERE list_id = OLD.list_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: concept list is locked'); END;

CREATE TRIGGER IF NOT EXISTS unit_content_locked BEFORE UPDATE ON unit_content
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: unit content is locked'); END;

CREATE TRIGGER IF NOT EXISTS unit_content_no_delete BEFORE DELETE ON unit_content
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: unit content is locked'); END;

CREATE TRIGGER IF NOT EXISTS practice_items_locked BEFORE UPDATE ON practice_items
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: practice item is locked'); END;

CREATE TRIGGER IF NOT EXISTS practice_items_no_delete BEFORE DELETE ON practice_items
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: practice item is locked'); END;

CREATE TRIGGER IF NOT EXISTS quiz_items_locked BEFORE UPDATE ON quiz_items
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: quiz item is locked'); END;

CREATE TRIGGER IF NOT EXISTS quiz_items_no_delete BEFORE DELETE ON quiz_items
WHEN OLD.locked_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'locked: quiz item is locked'); END;
