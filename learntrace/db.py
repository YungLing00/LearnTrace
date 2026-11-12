"""資料庫連線與結構。

三個 SQLite 檔分開存放：research.db（主庫）、ident.db（身分區）、ops.db（營運資料）。
同一條連線用 ATTACH 掛上另外兩個，讓一筆交易可以跨庫，但研究表裡只出現代號。
"""
import os
import sqlite3
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, 'schema.sql')

# 這些表只增不改：資料庫端直接拒絕 UPDATE 與 DELETE。
APPEND_ONLY = [
    'event_log', 'signal_events', 'questions', 'question_coding', 'quiz_responses',
    'teacher_predictions', 'quiz_confirmations', 'insight_cards', 'card_responses',
    'teacher_action_notes',
]

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _append_only_triggers():
    parts = []
    for t in APPEND_ONLY:
        parts.append(
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
            f"BEGIN SELECT RAISE(ABORT, 'locked: {t} is append-only'); END;"
        )
        parts.append(
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
            f"BEGIN SELECT RAISE(ABORT, 'locked: {t} is append-only'); END;"
        )
    return '\n'.join(parts)


def connect(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(
        os.path.join(data_dir, 'research.db'),
        isolation_level=None,          # 交易由 tx() 明確控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('ATTACH DATABASE ? AS ident', (os.path.join(data_dir, 'ident.db'),))
    conn.execute('ATTACH DATABASE ? AS ops', (os.path.join(data_dir, 'ops.db'),))
    return conn


def init_schema(conn):
    with open(SCHEMA_PATH, encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.executescript(_append_only_triggers())


def reset_files(data_dir):
    for name in ('research.db', 'ident.db', 'ops.db'):
        for suffix in ('', '-journal', '-wal', '-shm'):
            p = os.path.join(data_dir, name + suffix)
            if os.path.exists(p):
                os.remove(p)


@contextmanager
def tx(conn):
    """一筆交易：全部成功才寫入，任一步失敗全部取消。"""
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    else:
        conn.execute('COMMIT')
