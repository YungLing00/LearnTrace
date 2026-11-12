"""建立假資料與兩種測試情境。

情境 A：「時間常數 τ 的意義」真的多人卡關 → 應出 1 張洞察卡。
情境 B：沒有明顯卡關 → 不出卡。
學生行為一律透過 services 的同一套規則寫入，和真實操作走同一條路。

30 位假學生：
  idx 0–24  同意研究、完成單元、送出小考 → 本輪可分析學生 25 人
  idx 25    同意、完成單元、開始小考但沒送出（確認時變 expired）
  idx 26    同意、完成單元、沒開始小考
  idx 27    同意、沒完成單元（漏做第 3 個概念的練習）→ 伺服器不讓他進驗證小考
  idx 28–29 不同意研究：功能照常可用（也會按聽不懂、提問），但研究表不應出現任何資料
"""
import json
import random

from . import content as C
from . import services as S
from .db import connect, init_schema, reset_files, tx, NOW_SQL

ROUND_ID = 'R1'
TEACHER_ID = 'T01'
RESEARCHER_ID = 'RS01'
N_STUDENTS = 30
ANALYZABLE = list(range(25))


def student_id(i):
    return f'ST{i + 1:02d}'


def _codes(n, seed=2026):
    rnd = random.Random(seed)
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    seen = set()
    while len(seen) < n:
        seen.add('S-' + ''.join(rnd.choice(alphabet) for _ in range(5)))
    return sorted(seen, key=lambda _: rnd.random())


def seed_base(conn, lock_quiz):
    init_schema(conn)
    with tx(conn):
        conn.execute('INSERT INTO courses VALUES (?,?)', (C.COURSE['course_id'], C.COURSE['name']))
        conn.execute(f'INSERT INTO concept_lists VALUES (?,?,1,?,{NOW_SQL},{NOW_SQL})',
                     ('CL-RC-1', C.COURSE['course_id'], TEACHER_ID))
        for c in C.CONCEPTS:
            conn.execute('INSERT INTO concepts VALUES (?,?,?,?)', (c['concept_id'], 'CL-RC-1', c['seq'], c['name']))
        conn.execute('INSERT INTO rounds(round_id, course_id, round_no, unit_id, unit_name, content_version, '
                     "concept_list_id, stage) VALUES (?,?,1,?,?,1,'CL-RC-1','prediction')",
                     (ROUND_ID, C.COURSE['course_id'], C.UNIT['unit_id'], C.UNIT['unit_name']))
        for cid, levels in C.LEVELS.items():
            for lv, (title, body, example) in enumerate(levels, start=1):
                conn.execute(f'INSERT INTO unit_content VALUES (?,?,?,?,?,?,1,{NOW_SQL})',
                             (f'uc-{cid}-{lv}', cid, lv, title, body, example))
        for cid, p in C.PRACTICE.items():
            conn.execute(f'INSERT INTO practice_items VALUES (?,?,?,?,?,?,?,1,{NOW_SQL})',
                         (p['item_id'], cid, p['stem'], json.dumps(p['options'], ensure_ascii=False),
                          p['answer'], p['hint'], p['explanation']))
        for cid, items in C.QUIZ.items():
            for seq, q in enumerate(items, start=1):
                conn.execute('INSERT INTO quiz_items(quiz_item_id, round_id, concept_id, seq, stem, options, answer, '
                             'author_teacher_id, version) VALUES (?,?,?,?,?,?,?,?,1)',
                             (f'q-{cid}-{seq}', ROUND_ID, cid, seq, q['stem'],
                              json.dumps(q['options'], ensure_ascii=False), q['answer'], TEACHER_ID))
        if lock_quiz:
            conn.execute(f'UPDATE quiz_items SET locked_at={NOW_SQL} WHERE round_id=?', (ROUND_ID,))
        conn.execute("INSERT INTO ident.accounts VALUES (?,?, 'teacher', '示範教師')", (TEACHER_ID, 'teacher@demo'))
        conn.execute("INSERT INTO ident.accounts VALUES (?,?, 'researcher', '研究者')", (RESEARCHER_ID, 'researcher@demo'))
        for i, code in enumerate(_codes(N_STUDENTS)):
            sid = student_id(i)
            conn.execute("INSERT INTO ident.accounts VALUES (?,?, 'student', ?)",
                         (sid, f'student{i + 1:02d}@demo', f'學生 {i + 1:02d}'))
            conn.execute('INSERT INTO ident.research_codes(account_id, course_id, research_code) VALUES (?,?,?)',
                         (sid, C.COURSE['course_id'], code))


# 各情境的行為設定（idx 為學生編號 0–29）
R = lambda a, b: list(range(a, b + 1))
PLANS = {
    'A': {
        'predict': [('c2', '學生容易把充電與放電曲線搞混'), ('c3', '單位換算常出錯')],
        'practice_wrong': {'c1': R(0, 11) + R(25, 29), 'c2': R(0, 4), 'c3': R(0, 2)},
        'level_down': {'c1': R(5, 12) + [25, 28], 'c2': [20, 21]},
        'not_understood': {'c1': R(8, 14) + [26, 28]},
        'questions': {'c1': R(0, 3) + [27, 29]},
        'quiz_item_wrong': {'q-c1-1': R(0, 13), 'q-c1-2': R(0, 9), 'q-c2-1': R(0, 2), 'q-c2-2': R(10, 12),
                       'q-c3-1': [20, 21]},
        'quiz_blank': {'q-c3-2': [24]},
    },
    'B': {
        'predict': [('c1', '學生常記得公式但不懂物理意義'), ('c2', '曲線判讀容易混淆')],
        'practice_wrong': {'c1': R(0, 3) + [28], 'c2': [0, 1], 'c3': [0]},
        'level_down': {'c1': [4, 5]},
        'not_understood': {},
        'questions': {'c1': [0]},
        'quiz_item_wrong': {'q-c1-1': R(0, 2), 'q-c2-2': [5]},
        'quiz_blank': {},
    },
}

QUESTION_TEXTS = [
    '我知道公式，但不知道 τ 變大代表什麼',
    'τ 變大是充比較快還是比較慢？',
    '我是 115AC8020，想問 63% 是怎麼來的',      # 含學號，伺服器應去識別化
    '為什麼是 5τ 才算充滿？',
    'τ 跟電壓大小有關嗎？',
]


def _wrong_choice(options, answer):
    return next(o for o in options if o != answer)


def simulate(conn, scenario):
    plan = PLANS[scenario]
    S.submit_predictions(conn, TEACHER_ID, ROUND_ID,
                         [{'concept_id': c, 'reason': why} for c, why in plan['predict']])
    for i in range(N_STUDENTS):
        sid = student_id(i)
        S.set_consent(conn, sid, ROUND_ID, 'declined' if i >= 28 else 'consented')
        for cid in ('c1', 'c2', 'c3'):
            S.view_content(conn, sid, ROUND_ID, cid, 1)
            p = C.PRACTICE[cid]
            if i == 27 and cid == 'c3':
                continue                      # 漏做 → 無法完成單元
            if i in plan['practice_wrong'].get(cid, []):
                S.answer_practice(conn, sid, ROUND_ID, p['item_id'], _wrong_choice(p['options'], p['answer']))
            S.answer_practice(conn, sid, ROUND_ID, p['item_id'], p['answer'])
            if scenario == 'A' and i == 15 and cid == 'c1':   # 先答對再答錯 → 不算 practice_first_wrong
                S.answer_practice(conn, sid, ROUND_ID, p['item_id'], _wrong_choice(p['options'], p['answer']))
            if i in plan['level_down'].get(cid, []):
                S.level_down(conn, sid, ROUND_ID, cid)
                if i == 5:                    # 連按兩次 → 只記 1 次訊號
                    S.level_down(conn, sid, ROUND_ID, cid)
            if i in plan['not_understood'].get(cid, []):
                for _ in range(3 if i == 8 else 1):   # 狂按 → 只記 1 次
                    S.not_understood(conn, sid, ROUND_ID, cid)
            if i in plan['questions'].get(cid, []):
                S.submit_question(conn, sid, ROUND_ID, cid, QUESTION_TEXTS[i % len(QUESTION_TEXTS)])
        S.request_unit_complete(conn, sid, ROUND_ID)

    S.open_quiz(conn, TEACHER_ID, ROUND_ID)
    items = [(f'q-{cid}-{k + 1}', q) for cid, qs in C.QUIZ.items() for k, q in enumerate(qs)]
    for i in range(N_STUDENTS):
        sid = student_id(i)
        if i in (26, 27):        # 26 沒開始作答；27 沒完成單元，伺服器不讓他進小考
            continue
        S.quiz_start(conn, sid, ROUND_ID)
        if i == 25:
            continue
        answers = {}
        for qid, q in items:
            if i in plan['quiz_blank'].get(qid, []):
                answers[qid] = None
            elif i in plan['quiz_item_wrong'].get(qid, []):
                answers[qid] = _wrong_choice(q['options'], q['answer'])
            else:
                answers[qid] = q['answer']
        S.quiz_submit(conn, sid, ROUND_ID, answers)


def reset(data_dir, scenario='blank'):
    """scenario: 'blank'＝從教師事前預測開始；'A'／'B'＝已模擬到小考進行中，等教師確認。"""
    if scenario not in ('blank', 'A', 'B'):
        raise ValueError('scenario must be blank, A or B')
    reset_files(data_dir)
    conn = connect(data_dir)
    seed_base(conn, lock_quiz=scenario != 'blank')
    if scenario in PLANS:
        simulate(conn, scenario)
    return conn
