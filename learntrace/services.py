"""LearnTrace 的業務規則。HTTP 介面與模擬腳本都只透過這裡寫資料。

重點規則（對應〈資料表與事件紀錄規格〉）：
- 研究表只寫入 consent = consented 的學生；不同意者只更新營運層進度。
- 每種訊號同一學生、同一概念、同一輪只記 1 次（signal_events 主鍵去重）。
- practice_first_wrong 由伺服器對答案判定，只看該學生對該題的第一次作答。
- 洞察卡在教師確認小考資料時算一次並存成快照，之後不重算。
"""
import json
import re
import uuid

from .db import tx, NOW_SQL
from . import content as C

STAGES = ['prediction', 'learning', 'quiz', 'quiz_confirmed', 'card_open', 'closed']
SIGNALS = ['practice_first_wrong', 'level_down', 'not_understood', 'question_submitted']
SIGNAL_LABELS = {
    'practice_first_wrong': '課中練習首次答錯',
    'level_down': '要求簡化說明',
    'not_understood': '按「聽不懂」',
    'question_submitted': '留下相關問題',
}
NOT_STORED_NOTICE = '此問題不會被儲存或納入研究；如需協助請使用課程既有提問管道。'
NOT_RECORDED_NOTICE = '你未參與研究，這個按鈕不會被記錄或納入研究；如需協助請使用課程既有提問管道。'
RESPONSES = {'adopt': '採納', 'later': '延後', 'known': '早就知道', 'disagree': '不同意'}
MIN_COUNT = 5          # 少於 5 人不顯示精確人數、不出卡
RATIO_NUM, RATIO_DEN = 3, 10   # 30%


class LTError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ── 共用 ────────────────────────────────────────────────
def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def get_round(conn, round_id):
    r = one(conn, 'SELECT * FROM rounds WHERE round_id = ?', (round_id,))
    if not r:
        raise LTError(404, '找不到這一輪')
    return r


def require_stage(r, *stages):
    if r['stage'] not in stages:
        raise LTError(409, f'目前階段是「{stage_label(r["stage"])}」，不能做這個動作')


def stage_label(stage):
    return {
        'prediction': '教師事前預測', 'learning': '學生學習中', 'quiz': '驗證小考進行中',
        'quiz_confirmed': '小考資料已確認', 'card_open': '洞察卡已開放', 'closed': '本輪已結束',
    }[stage]


def get_account(conn, account_id, role=None):
    a = one(conn, 'SELECT * FROM ident.accounts WHERE account_id = ?', (account_id,))
    if not a:
        raise LTError(403, '找不到這個帳號')
    if role and a['role'] != role:
        raise LTError(403, '這個帳號沒有權限做這個動作')
    return a


def concept_rows(conn, r):
    return rows(conn, 'SELECT * FROM concepts WHERE list_id = ? ORDER BY seq', (r['concept_list_id'],))


def check_concept(conn, r, concept_id):
    c = one(conn, 'SELECT * FROM concepts WHERE concept_id = ? AND list_id = ?',
            (concept_id, r['concept_list_id']))
    if not c:
        raise LTError(400, '不是本單元的概念')
    return c


def concept_list_version(conn, r):
    return one(conn, 'SELECT version FROM concept_lists WHERE list_id = ?', (r['concept_list_id'],))['version']


def ceil_div(a, b):
    return -(-a // b)


def card_threshold(n):
    """出卡門檻：至少 5 人，且達可分析學生的 30%（無條件進位）。"""
    return max(MIN_COUNT, ceil_div(RATIO_NUM * n, RATIO_DEN))


# ── 學生端 ─────────────────────────────────────────────
def student_ctx(conn, account_id, round_id):
    get_account(conn, account_id, 'student')
    r = get_round(conn, round_id)
    rc = one(conn, 'SELECT * FROM ident.research_codes WHERE account_id = ? AND course_id = ?',
             (account_id, r['course_id']))
    if not rc:
        raise LTError(403, '沒有這門課的修課紀錄')
    return r, rc


def research_code(rc):
    """只有同意研究的學生才有研究代號可以寫入研究表。"""
    return rc['research_code'] if rc['consent'] == 'consented' else None


def require_consent_decided(rc):
    if rc['consent'] == 'pending':
        raise LTError(409, '請先選擇是否參與研究')


def ensure_progress(conn, account_id, round_id, concept_id):
    conn.execute('INSERT OR IGNORE INTO ops.app_progress(account_id, round_id, concept_id) VALUES (?,?,?)',
                 (account_id, round_id, concept_id))
    return one(conn, 'SELECT * FROM ops.app_progress WHERE account_id=? AND round_id=? AND concept_id=?',
               (account_id, round_id, concept_id))


def log_event(conn, r, code, concept_id, event_type, payload=None):
    cur = conn.execute(
        'INSERT INTO event_log(round_id, research_code, concept_id, event_type, payload, content_version) '
        'VALUES (?,?,?,?,?,?)',
        (r['round_id'], code, concept_id, event_type,
         json.dumps(payload or {}, ensure_ascii=False), r['content_version']))
    return cur.lastrowid


def add_signal(conn, r, code, concept_id, signal_type, log_id):
    """已存在就不再寫（去重）。回傳這次是否新記一筆。"""
    cur = conn.execute(
        'INSERT OR IGNORE INTO signal_events(round_id, research_code, concept_id, signal_type, first_at, source_log_id) '
        'SELECT ?, ?, ?, ?, server_at, log_id FROM event_log WHERE log_id = ?',
        (r['round_id'], code, concept_id, signal_type, log_id))
    return cur.rowcount == 1


def set_consent(conn, account_id, round_id, decision):
    if decision not in ('consented', 'declined'):
        raise LTError(400, '只能選擇參與或不參與')
    r, rc = student_ctx(conn, account_id, round_id)
    if rc['consent'] != 'pending':
        raise LTError(409, '已經選擇過；撤回同意依倫理審查核准的程序處理')
    with tx(conn):
        conn.execute(
            f'UPDATE ident.research_codes SET consent = ?, consent_at = {NOW_SQL} '
            'WHERE account_id = ? AND course_id = ?', (decision, account_id, r['course_id']))
    return {'consent': decision}


def student_state(conn, account_id, round_id):
    r, rc = student_ctx(conn, account_id, round_id)
    progress = {}
    for c in concept_rows(conn, r):
        p = one(conn, 'SELECT * FROM ops.app_progress WHERE account_id=? AND round_id=? AND concept_id=?',
                (account_id, round_id, c['concept_id']))
        progress[c['concept_id']] = {
            'level1_viewed': bool(p and p['level1_viewed_at']),
            'practice_answered': bool(p and p['practice_answered_at']),
            'current_level': p['current_level'] if p else 1,
        }
    u = one(conn, 'SELECT * FROM ops.app_unit WHERE account_id=? AND round_id=?', (account_id, round_id))
    return {
        'stage': r['stage'], 'stage_label': stage_label(r['stage']),
        'unit_name': r['unit_name'], 'consent': rc['consent'],
        'progress': progress,
        'unit_completed': bool(u and u['unit_completed_at']),
        'quiz_completed': bool(u and u['quiz_completed_at']),
    }


def unit_material(conn, account_id, round_id):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    out = []
    for c in concept_rows(conn, r):
        levels = rows(conn, 'SELECT level, title, body, example FROM unit_content WHERE concept_id=? '
                            'AND version=? ORDER BY level', (c['concept_id'], r['content_version']))
        p = one(conn, 'SELECT item_id, stem, options FROM practice_items WHERE concept_id=? AND version=?',
                (c['concept_id'], r['content_version']))
        out.append({
            'concept_id': c['concept_id'], 'name': c['name'],
            'levels': [dict(l) for l in levels],
            'practice': {'item_id': p['item_id'], 'stem': p['stem'], 'options': json.loads(p['options'])},
        })
    return {'concepts': out, 'content_version': r['content_version']}


def view_content(conn, account_id, round_id, concept_id, level):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    require_consent_decided(rc)
    check_concept(conn, r, concept_id)
    if level not in (1, 2, 3):
        raise LTError(400, '說明只有三層')
    with tx(conn):
        p = ensure_progress(conn, account_id, round_id, concept_id)
        if level > p['current_level']:
            raise LTError(409, '要先按「看更簡單的說明」')
        if level == 1:
            conn.execute(
                f'UPDATE ops.app_progress SET level1_viewed_at = COALESCE(level1_viewed_at, {NOW_SQL}), '
                'current_level = 1 WHERE account_id=? AND round_id=? AND concept_id=?',
                (account_id, round_id, concept_id))
        code = research_code(rc)
        if code:
            log_event(conn, r, code, concept_id, 'content_viewed', {'level': level})
    return {'level': 1 if level == 1 else p['current_level']}


def answer_practice(conn, account_id, round_id, item_id, choice):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    require_consent_decided(rc)
    item = one(conn, 'SELECT p.* FROM practice_items p JOIN concepts c ON c.concept_id = p.concept_id '
                     'WHERE p.item_id = ? AND c.list_id = ? AND p.version = ?',
               (item_id, r['concept_list_id'], r['content_version']))
    if not item:
        raise LTError(400, '找不到這題練習')
    if choice not in json.loads(item['options']):
        raise LTError(400, '不是這題的選項')
    correct = choice == item['answer']          # 對錯由伺服器判定
    concept_id = item['concept_id']
    with tx(conn):
        ensure_progress(conn, account_id, round_id, concept_id)
        conn.execute(
            f'UPDATE ops.app_progress SET practice_answered_at = COALESCE(practice_answered_at, {NOW_SQL}) '
            'WHERE account_id=? AND round_id=? AND concept_id=?', (account_id, round_id, concept_id))
        code = research_code(rc)
        if code:
            prior = one(conn, "SELECT COUNT(*) AS n FROM event_log WHERE round_id=? AND research_code=? "
                              "AND event_type='practice_answer' AND json_extract(payload,'$.item_id')=?",
                        (round_id, code, item_id))['n']
            log_id = log_event(conn, r, code, concept_id, 'practice_answer',
                               {'item_id': item_id, 'choice': choice, 'correct': correct})
            if prior == 0 and not correct:
                add_signal(conn, r, code, concept_id, 'practice_first_wrong', log_id)
    return {'correct': correct, 'message': item['explanation'] if correct else item['hint']}


def level_down(conn, account_id, round_id, concept_id):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    require_consent_decided(rc)
    check_concept(conn, r, concept_id)
    with tx(conn):
        p = ensure_progress(conn, account_id, round_id, concept_id)
        cur = p['current_level']
        if cur >= 3:
            raise LTError(409, '已是最簡單的說明')
        conn.execute('UPDATE ops.app_progress SET current_level = ? WHERE account_id=? AND round_id=? AND concept_id=?',
                     (cur + 1, account_id, round_id, concept_id))
        code = research_code(rc)
        if code:
            log_id = log_event(conn, r, code, concept_id, 'level_down', {'from_level': cur, 'to_level': cur + 1})
            add_signal(conn, r, code, concept_id, 'level_down', log_id)
    return {'level': cur + 1}


def not_understood(conn, account_id, round_id, concept_id):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    check_concept(conn, r, concept_id)
    require_consent_decided(rc)
    code = research_code(rc)
    if not code:
        # 不同意研究者：按鈕照常提供，但不寫入任何資料庫
        return {'recorded': False, 'message': NOT_RECORDED_NOTICE}
    with tx(conn):
        p = ensure_progress(conn, account_id, round_id, concept_id)
        log_id = log_event(conn, r, code, concept_id, 'not_understood', {'level': p['current_level']})
        add_signal(conn, r, code, concept_id, 'not_understood', log_id)
    return {'recorded': True, 'message': '已記錄。你可以看更簡單的說明，或留下問題。'}


PII_PATTERNS = [
    re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'),           # email
    re.compile(r'09\d{2}[- ]?\d{3}[- ]?\d{3}'),             # 手機
    re.compile(r'(?<![0-9A-Za-z])\d{3}[A-Za-z]{2}\d{3,4}(?![0-9A-Za-z])'),  # 學號樣式
    re.compile(r'\d{8,}'),                                  # 長串數字
]


def redact(text):
    """移除可識別資訊。姓名無法可靠偵測，正式版需再搭配人工檢查。"""
    out, flag = text, False
    for pat in PII_PATTERNS:
        out, n = pat.subn('［已移除］', out)
        flag = flag or n > 0
    return out, flag


def submit_question(conn, account_id, round_id, concept_id, text):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    check_concept(conn, r, concept_id)
    require_consent_decided(rc)
    text = (text or '').strip()
    if not text or len(text) > 500:
        raise LTError(400, '問題要有內容，且不超過 500 字')
    code = research_code(rc)
    if not code:
        # 資料最小化：不同意研究者的問題文字不寫入任何資料庫，回應後即丟棄
        return {'recorded': False, 'message': NOT_STORED_NOTICE}
    redacted, flag = redact(text)
    qid = 'Q-' + uuid.uuid4().hex[:12]                     # 由伺服器建立
    with tx(conn):
        conn.execute('INSERT INTO questions(question_id, round_id, research_code, concept_id, text_raw, '
                     'text_redacted, pii_flag) VALUES (?,?,?,?,?,?,?)',
                     (qid, round_id, code, concept_id, text, redacted, int(flag)))
        log_id = log_event(conn, r, code, concept_id, 'question_submitted', {'question_id': qid})
        add_signal(conn, r, code, concept_id, 'question_submitted', log_id)
    return {'recorded': True, 'message': '已記錄你的問題'}


def request_unit_complete(conn, account_id, round_id):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'learning')
    require_consent_decided(rc)
    with tx(conn):
        missing = []
        for c in concept_rows(conn, r):
            p = ensure_progress(conn, account_id, round_id, c['concept_id'])
            if not p['level1_viewed_at']:
                missing.append(f'{c["name"]}：還沒看第 1 層說明')
            if not p['practice_answered_at']:
                missing.append(f'{c["name"]}：還沒做課中練習')
        if missing:
            return {'completed': False, 'missing': missing}
        conn.execute('INSERT OR IGNORE INTO ops.app_unit(account_id, round_id) VALUES (?,?)', (account_id, round_id))
        conn.execute(f'UPDATE ops.app_unit SET unit_completed_at = COALESCE(unit_completed_at, {NOW_SQL}) '
                     'WHERE account_id=? AND round_id=?', (account_id, round_id))
        code = research_code(rc)
        if code:
            done = one(conn, "SELECT 1 FROM event_log WHERE round_id=? AND research_code=? "
                             "AND event_type='unit_completed'", (round_id, code))
            if not done:      # 由伺服器建立，前端不能直接送 unit_completed
                log_event(conn, r, code, None, 'unit_completed', {'unit_id': r['unit_id']})
    return {'completed': True, 'missing': []}


def unit_completed(conn, account_id, round_id):
    u = one(conn, 'SELECT unit_completed_at FROM ops.app_unit WHERE account_id=? AND round_id=?',
            (account_id, round_id))
    return bool(u and u['unit_completed_at'])


def require_unit_completed(conn, account_id, round_id):
    """「完成單元 → 驗證小考」是伺服器規則，不只是畫面流程。"""
    if not unit_completed(conn, account_id, round_id):
        raise LTError(409, '要先完成本單元，才能作答驗證小考')


def quiz_view(conn, account_id, round_id):
    r, rc = student_ctx(conn, account_id, round_id)
    done_unit = unit_completed(conn, account_id, round_id)
    status = 'not_started'
    code = research_code(rc)
    if code:
        a = one(conn, 'SELECT status FROM quiz_attempts WHERE round_id=? AND research_code=?', (round_id, code))
        status = a['status'] if a else 'not_started'
    else:
        u = one(conn, 'SELECT quiz_completed_at FROM ops.app_unit WHERE account_id=? AND round_id=?',
                (account_id, round_id))
        status = 'submitted' if u and u['quiz_completed_at'] else 'not_started'
    items = []
    if r['stage'] == 'quiz' and done_unit and status in ('not_started', 'started'):
        for q in rows(conn, 'SELECT q.quiz_item_id, q.stem, q.options FROM quiz_items q '
                            'JOIN concepts c ON c.concept_id = q.concept_id '
                            'WHERE q.round_id = ? ORDER BY c.seq, q.seq', (round_id,)):
            items.append({'quiz_item_id': q['quiz_item_id'], 'stem': q['stem'],
                          'options': json.loads(q['options'])})   # 不含答案與概念
    return {'stage': r['stage'], 'status': status, 'unit_completed': done_unit, 'items': items}


def quiz_start(conn, account_id, round_id):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'quiz')
    require_consent_decided(rc)
    require_unit_completed(conn, account_id, round_id)
    code = research_code(rc)
    if code:
        with tx(conn):
            conn.execute('INSERT OR IGNORE INTO quiz_attempts(round_id, research_code) VALUES (?,?)', (round_id, code))
    return quiz_view(conn, account_id, round_id)


def quiz_submit(conn, account_id, round_id, answers):
    r, rc = student_ctx(conn, account_id, round_id)
    require_stage(r, 'quiz')
    require_consent_decided(rc)
    require_unit_completed(conn, account_id, round_id)
    items = rows(conn, 'SELECT * FROM quiz_items WHERE round_id = ?', (round_id,))
    by_id = {q['quiz_item_id']: q for q in items}
    answers = answers or {}
    for k, v in answers.items():
        if k not in by_id:
            raise LTError(400, '有不屬於這份小考的題目')
        if v is not None and v not in json.loads(by_id[k]['options']):
            raise LTError(400, '有不是選項的答案')
    code = research_code(rc)
    with tx(conn):
        conn.execute('INSERT OR IGNORE INTO ops.app_unit(account_id, round_id) VALUES (?,?)', (account_id, round_id))
        u = one(conn, 'SELECT quiz_completed_at FROM ops.app_unit WHERE account_id=? AND round_id=?',
                (account_id, round_id))
        if u['quiz_completed_at']:
            raise LTError(409, '已經送出過小考')
        if code:
            a = one(conn, 'SELECT status FROM quiz_attempts WHERE round_id=? AND research_code=?', (round_id, code))
            if not a or a['status'] != 'started':
                raise LTError(409, '要先開始作答，且只能送出一次')
            for q in items:
                choice = answers.get(q['quiz_item_id'])
                correct = None if choice is None else int(choice == q['answer'])
                conn.execute('INSERT INTO quiz_responses(round_id, research_code, quiz_item_id, choice, correct) '
                             'VALUES (?,?,?,?,?)', (round_id, code, q['quiz_item_id'], choice, correct))
            conn.execute(f"UPDATE quiz_attempts SET status='submitted', submitted_at={NOW_SQL} "
                         "WHERE round_id=? AND research_code=? AND status='started'", (round_id, code))
        # 不同意研究者：只留完成時間，作答內容不寫入資料庫
        conn.execute(f'UPDATE ops.app_unit SET quiz_completed_at = {NOW_SQL} WHERE account_id=? AND round_id=?',
                     (account_id, round_id))
    return {'submitted': True}


# ── 教師端 ─────────────────────────────────────────────
def teacher_overview(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    preds = one(conn, 'SELECT COUNT(*) AS n FROM teacher_predictions WHERE round_id=?', (round_id,))['n']
    q = one(conn, 'SELECT COUNT(*) AS n, SUM(locked_at IS NOT NULL) AS locked FROM quiz_items WHERE round_id=?',
            (round_id,))
    return {
        'round_id': round_id, 'round_no': r['round_no'], 'unit_name': r['unit_name'],
        'stage': r['stage'], 'stage_label': stage_label(r['stage']),
        'predictions_submitted': preds > 0,
        'quiz_items_locked': q['n'] > 0 and q['locked'] == q['n'],
        'concept_list_version': concept_list_version(conn, r),
    }


def teacher_concepts(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    return {'concepts': [dict(c) for c in concept_rows(conn, r)],
            'concept_list_version': concept_list_version(conn, r)}


def get_predictions(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    out = rows(conn, 'SELECT p.rank, p.concept_id, c.name, p.reason, p.submitted_at, p.prediction_version, '
                     'p.concept_list_version FROM teacher_predictions p JOIN concepts c ON c.concept_id=p.concept_id '
                     'WHERE p.round_id=? ORDER BY p.rank', (round_id,))
    return {'predictions': [dict(x) for x in out]}


def submit_predictions(conn, teacher_id, round_id, preds):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    require_stage(r, 'prediction')
    if not isinstance(preds, list) or not 2 <= len(preds) <= 3:
        raise LTError(400, '至少選 2 個、最多 3 個概念')
    ids = [p.get('concept_id') for p in preds]
    if len(set(ids)) != len(ids):
        raise LTError(400, '同一個概念不能選兩次')
    for cid in ids:
        check_concept(conn, r, cid)
    lst = one(conn, 'SELECT * FROM concept_lists WHERE list_id=?', (r['concept_list_id'],))
    if not lst['locked_at']:
        raise LTError(409, '概念清單還沒經教師審核鎖定')
    with tx(conn):
        if one(conn, 'SELECT 1 FROM teacher_predictions WHERE round_id=?', (round_id,)):
            raise LTError(409, '本輪預測已送出，不能修改')
        for i, p in enumerate(preds, start=1):
            reason = (p.get('reason') or '').strip()[:300] or None
            conn.execute('INSERT INTO teacher_predictions(round_id, teacher_id, rank, concept_id, reason, '
                         'prediction_version, concept_list_version) VALUES (?,?,?,?,?,1,?)',
                         (round_id, teacher_id, i, p['concept_id'], reason, lst['version']))
        # 預測送出後才開放學生學習
        conn.execute(f"UPDATE rounds SET stage='learning', learning_opened_at={NOW_SQL} WHERE round_id=?",
                     (round_id,))
    return get_predictions(conn, teacher_id, round_id)


def teacher_quiz_items(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    out = rows(conn, 'SELECT q.*, c.name AS concept_name FROM quiz_items q JOIN concepts c '
                     'ON c.concept_id=q.concept_id WHERE q.round_id=? ORDER BY c.seq, q.seq', (round_id,))
    return {'items': [dict(x, options=json.loads(x['options'])) for x in out]}


def update_quiz_item(conn, teacher_id, quiz_item_id, stem, options, answer):
    get_account(conn, teacher_id, 'teacher')
    q = one(conn, 'SELECT * FROM quiz_items WHERE quiz_item_id=?', (quiz_item_id,))
    if not q:
        raise LTError(404, '找不到這題')
    if q['locked_at']:
        raise LTError(409, '小考題目已鎖定，不能修改')
    stem = (stem or '').strip()
    if not stem or not isinstance(options, list) or len(options) != 4 \
            or any(not str(o).strip() for o in options) or len(set(options)) != 4:
        raise LTError(400, '每題要有題幹和 4 個不重複的選項')
    if answer not in options:
        raise LTError(400, '正確答案必須是其中一個選項')
    with tx(conn):
        conn.execute('UPDATE quiz_items SET stem=?, options=?, answer=?, author_teacher_id=? WHERE quiz_item_id=?',
                     (stem, json.dumps(options, ensure_ascii=False), answer, teacher_id, quiz_item_id))
    return {'ok': True}


def lock_quiz_items(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    require_stage(r, 'prediction', 'learning')
    for c in concept_rows(conn, r):
        n = one(conn, 'SELECT COUNT(*) AS n FROM quiz_items WHERE round_id=? AND concept_id=?',
                (round_id, c['concept_id']))['n']
        if n != 2:
            raise LTError(400, f'「{c["name"]}」要剛好 2 題驗證小考')
    with tx(conn):
        conn.execute(f'UPDATE quiz_items SET locked_at={NOW_SQL} WHERE round_id=? AND locked_at IS NULL', (round_id,))
    return teacher_overview(conn, teacher_id, round_id)


def open_quiz(conn, teacher_id, round_id):
    ov = teacher_overview(conn, teacher_id, round_id)
    r = get_round(conn, round_id)
    require_stage(r, 'learning')
    if not ov['quiz_items_locked']:
        raise LTError(409, '請先確認並鎖定驗證小考題目')
    with tx(conn):
        conn.execute(f"UPDATE rounds SET stage='quiz', quiz_opened_at={NOW_SQL} WHERE round_id=?", (round_id,))
    return teacher_overview(conn, teacher_id, round_id)


def _quiz_counts(conn, r):
    consented_n = one(conn, "SELECT COUNT(*) AS n FROM ident.research_codes WHERE course_id=? AND consent='consented'",
                      (r['course_id'],))['n']
    completed_n = one(conn, "SELECT COUNT(*) AS n FROM quiz_attempts WHERE round_id=? AND status='submitted'",
                      (r['round_id'],))['n']
    missing = one(conn, 'SELECT COUNT(*) AS n FROM quiz_responses WHERE round_id=? AND choice IS NULL',
                  (r['round_id'],))['n']
    return consented_n, completed_n, consented_n - completed_n, missing


def quiz_status(conn, teacher_id, round_id):
    """只給人數，不給名單、代號、答對率或各題結果。"""
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    consented_n, completed_n, not_completed_n, missing = _quiz_counts(conn, r)
    mapping = rows(conn, 'SELECT q.seq, c.seq AS cseq, c.name FROM quiz_items q JOIN concepts c '
                         'ON c.concept_id=q.concept_id WHERE q.round_id=? ORDER BY c.seq, q.seq', (round_id,))
    return {
        'stage': r['stage'], 'consented_n': consented_n, 'completed_n': completed_n,
        'not_completed_n': not_completed_n, 'missing_cells': missing,
        'mapping': [{'no': i + 1, 'concept_name': m['name']} for i, m in enumerate(mapping)],
        'concept_list_version': concept_list_version(conn, r),
    }


def confirm_quiz(conn, teacher_id, round_id, map_ok, closed_ok):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    require_stage(r, 'quiz')
    if map_ok is not True or closed_ok is not True:
        raise LTError(400, '兩個確認欄都要勾選')
    with tx(conn):
        conn.execute("UPDATE quiz_attempts SET status='expired' WHERE round_id=? AND status='started'", (round_id,))
        consented_n, completed_n, not_completed_n, missing = _quiz_counts(conn, r)
        conn.execute('INSERT INTO quiz_confirmations(round_id, teacher_id, map_ok, closed_ok, quiz_data_version, '
                     'concept_list_version, consented_n, completed_n, not_completed_n, missing_cells) '
                     'VALUES (?,?,1,1,1,?,?,?,?,?)',
                     (round_id, teacher_id, concept_list_version(conn, r), consented_n, completed_n,
                      not_completed_n, missing))
        conn.execute(f"UPDATE rounds SET stage='quiz_confirmed', quiz_confirmed_at={NOW_SQL} WHERE round_id=?",
                     (round_id,))
        n_cards = generate_cards(conn, get_round(conn, round_id))
        conn.execute(f"UPDATE rounds SET stage='card_open', card_opened_at={NOW_SQL} WHERE round_id=?",
                     (round_id,))
    return {'cards': n_cards}


# ── 洞察卡計算 ─────────────────────────────────────────
def concept_stats(conn, r):
    """從事件紀錄算出每個概念、每種訊號是哪些可分析學生，以及來源事件。"""
    consented = {x['research_code'] for x in rows(
        conn, "SELECT research_code FROM ident.research_codes WHERE course_id=? AND consent='consented'",
        (r['course_id'],))}
    completed = {x['research_code'] for x in rows(
        conn, "SELECT DISTINCT research_code FROM event_log WHERE round_id=? AND event_type='unit_completed'",
        (r['round_id'],))}
    submitted = {x['research_code'] for x in rows(
        conn, "SELECT research_code FROM quiz_attempts WHERE round_id=? AND status='submitted'", (r['round_id'],))}
    analyzable = consented & completed & submitted
    per = {c['concept_id']: {s: {} for s in SIGNALS} for c in concept_rows(conn, r)}
    cutoff = r['quiz_opened_at']
    for s in rows(conn, 'SELECT * FROM signal_events WHERE round_id=? AND (? IS NULL OR first_at <= ?)',
                  (r['round_id'], cutoff, cutoff)):
        if s['research_code'] in analyzable and s['concept_id'] in per:
            per[s['concept_id']][s['signal_type']][s['research_code']] = s['source_log_id']
    return {'consented_n': len(consented), 'analyzable': analyzable, 'per_concept': per}


def build_display(counts, n):
    out = []
    for s in SIGNALS:
        k = counts[s]
        if k < MIN_COUNT:
            out.append({'signal': s, 'label': SIGNAL_LABELS[s], 'masked': True, 'text': '少於 5 人'})
        else:
            out.append({'signal': s, 'label': SIGNAL_LABELS[s], 'masked': False, 'n': k,
                        'pct': round(100 * k / n, 1), 'text': f'{k} / {n} 人（{round(100 * k / n, 1)}%）'})
    return out


def generate_cards(conn, r):
    stats = concept_stats(conn, r)
    n = len(stats['analyzable'])
    if n == 0:
        return 0
    need = card_threshold(n)
    made = 0
    for c in concept_rows(conn, r):
        per = stats['per_concept'][c['concept_id']]
        union = set().union(*[set(per[s]) for s in SIGNALS])
        if len(union) < need:
            continue
        counts = {s: len(per[s]) for s in SIGNALS}
        askers = counts['question_submitted']
        display = {
            'concept_name': c['name'],
            'denominator_text': f'本輪可分析學生 {n} / {stats["consented_n"]} 人',
            'denominator_note': f'{n}＝完成單元與驗證小考；{stats["consented_n"]}＝同意研究',
            'signals': build_display(counts, n),
        }
        conn.execute(
            'INSERT INTO insight_cards(card_id, round_id, concept_id, analyzable_n, consented_n, counts, display, '
            'askers_n, confusion_summary, ai_suggestion, ai_model, prompt_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (f'card-{r["round_id"]}-{c["concept_id"]}', r['round_id'], c['concept_id'], n, stats['consented_n'],
             json.dumps(counts), json.dumps(display, ensure_ascii=False), askers,
             C.CONFUSION.get(c['concept_id']) if askers >= MIN_COUNT else None,
             C.SUGGESTION.get(c['concept_id'], ''), C.AI_MODEL, C.PROMPT_VERSION))
        made += 1
    return made


def teacher_cards(conn, teacher_id, round_id):
    get_account(conn, teacher_id, 'teacher')
    r = get_round(conn, round_id)
    require_stage(r, 'card_open', 'closed')
    out = []
    for card in rows(conn, 'SELECT k.*, c.seq FROM insight_cards k JOIN concepts c ON c.concept_id=k.concept_id '
                           'WHERE k.round_id=? ORDER BY c.seq', (round_id,)):
        resp = one(conn, 'SELECT response, note, submitted_at FROM card_responses WHERE card_id=?', (card['card_id'],))
        notes = rows(conn, 'SELECT note, server_at FROM teacher_action_notes WHERE card_id=? ORDER BY note_id',
                     (card['card_id'],))
        out.append({
            'card_id': card['card_id'], 'unit_name': r['unit_name'], 'display': json.loads(card['display']),
            'confusion_summary': card['confusion_summary'], 'ai_suggestion': card['ai_suggestion'],
            'response': dict(resp, label=RESPONSES[resp['response']]) if resp else None,
            'action_notes': [dict(x) for x in notes],
        })
    return {'cards': out, 'analyzable_rule': '至少 5 人，且達可分析學生的 30%'}


def respond_card(conn, teacher_id, card_id, response, note):
    get_account(conn, teacher_id, 'teacher')
    card = one(conn, 'SELECT * FROM insight_cards WHERE card_id=?', (card_id,))
    if not card:
        raise LTError(404, '找不到這張卡')
    require_stage(get_round(conn, card['round_id']), 'card_open')
    if response not in RESPONSES:
        raise LTError(400, '請選擇採納、延後、早就知道或不同意')
    note = (note or '').strip()
    if not note:
        raise LTError(400, '請填寫處理方式或判斷原因')
    with tx(conn):
        if one(conn, 'SELECT 1 FROM card_responses WHERE card_id=?', (card_id,)):
            raise LTError(409, '這張卡已經回應過，不能修改；後續做法請用補記')
        conn.execute('INSERT INTO card_responses(card_id, teacher_id, response, note) VALUES (?,?,?,?)',
                     (card_id, teacher_id, response, note[:1000]))
    return {'ok': True}


def add_action_note(conn, teacher_id, card_id, note):
    get_account(conn, teacher_id, 'teacher')
    if not one(conn, 'SELECT 1 FROM card_responses WHERE card_id=?', (card_id,)):
        raise LTError(409, '要先送出回應，才能補記')
    note = (note or '').strip()
    if not note:
        raise LTError(400, '補記要有內容')
    with tx(conn):
        conn.execute('INSERT INTO teacher_action_notes(card_id, note) VALUES (?,?)', (card_id, note[:1000]))
    return {'ok': True}


# ── 研究者 ─────────────────────────────────────────────
def trace_card(conn, researcher_id, card_id):
    """把洞察卡上的每個數字追回到 event_log 的原始紀錄。"""
    get_account(conn, researcher_id, 'researcher')
    card = one(conn, 'SELECT * FROM insight_cards WHERE card_id=?', (card_id,))
    if not card:
        raise LTError(404, '找不到這張卡')
    r = get_round(conn, card['round_id'])
    stats = concept_stats(conn, r)
    snap = json.loads(card['counts'])
    per = stats['per_concept'][card['concept_id']]
    signals = []
    for s in SIGNALS:
        evidence = []
        for code, log_id in sorted(per[s].items()):
            e = one(conn, 'SELECT log_id, event_type, payload, server_at FROM event_log WHERE log_id=?', (log_id,))
            evidence.append({'research_code': code, 'log_id': e['log_id'], 'event_type': e['event_type'],
                             'server_at': e['server_at'], 'payload': json.loads(e['payload'])})
        signals.append({'signal': s, 'label': SIGNAL_LABELS[s], 'card_count': snap[s],
                        'recomputed': len(evidence), 'match': snap[s] == len(evidence), 'evidence': evidence})
    return {
        'card_id': card_id, 'concept_id': card['concept_id'],
        'analyzable_n': card['analyzable_n'], 'analyzable_recomputed': len(stats['analyzable']),
        'consented_n': card['consented_n'], 'generated_at': card['generated_at'],
        'signals': signals,
        'all_match': all(x['match'] for x in signals) and card['analyzable_n'] == len(stats['analyzable']),
    }


def research_cards(conn, researcher_id, round_id):
    get_account(conn, researcher_id, 'researcher')
    r = get_round(conn, round_id)
    cards = rows(conn, 'SELECT card_id FROM insight_cards WHERE round_id=?', (round_id,))
    stats = concept_stats(conn, r)
    n = len(stats['analyzable'])
    summary = []
    for c in concept_rows(conn, r):
        per = stats['per_concept'][c['concept_id']]
        union = set().union(*[set(per[s]) for s in SIGNALS])
        summary.append({'concept_id': c['concept_id'], 'name': c['name'],
                        'counts': {s: len(per[s]) for s in SIGNALS}, 'union': len(union)})
    return {
        'stage': r['stage'], 'analyzable_n': n, 'consented_n': stats['consented_n'],
        'threshold': card_threshold(n) if n else None, 'concepts': summary,
        'traces': [trace_card(conn, researcher_id, x['card_id']) for x in cards],
    }


EXPORT_FIELDS = ['research_code', 'round_id', 'concept_id',
                 'practice_first_wrong', 'level_down', 'not_understood', 'question_submitted',
                 'practice_first_wrong_first_at', 'level_down_first_at', 'not_understood_first_at',
                 'question_submitted_first_at',
                 'quiz_correct_n', 'quiz_item_n', 'quiz_missing_n', 'quiz_score',
                 'teacher_predicted', 'on_card']


def export_rows(conn, researcher_id, round_id):
    """RQ1 主表：一列一個「可分析學生 × 概念」。只含代號。"""
    get_account(conn, researcher_id, 'researcher')
    r = get_round(conn, round_id)
    stats = concept_stats(conn, r)
    predicted = {x['concept_id'] for x in rows(conn, 'SELECT concept_id FROM teacher_predictions WHERE round_id=?',
                                                (round_id,))}
    on_card = {x['concept_id'] for x in rows(conn, 'SELECT concept_id FROM insight_cards WHERE round_id=?',
                                              (round_id,))}
    first = {(x['research_code'], x['concept_id'], x['signal_type']): x['first_at']
             for x in rows(conn, 'SELECT * FROM signal_events WHERE round_id=?', (round_id,))}
    out = []
    for code in sorted(stats['analyzable']):
        for c in concept_rows(conn, r):
            cid = c['concept_id']
            per = stats['per_concept'][cid]
            resp = rows(conn, 'SELECT r.choice, r.correct FROM quiz_responses r JOIN quiz_items q '
                              'ON q.quiz_item_id = r.quiz_item_id WHERE r.round_id=? AND r.research_code=? '
                              'AND q.concept_id=?', (round_id, code, cid))
            item_n = len(resp)
            missing = sum(1 for x in resp if x['choice'] is None)
            correct = sum(1 for x in resp if x['correct'] == 1)
            row = {'research_code': code, 'round_id': round_id, 'concept_id': cid}
            for s in SIGNALS:
                row[s] = int(code in per[s])
                row[f'{s}_first_at'] = first.get((code, cid, s), '') if code in per[s] else ''
            row.update({
                'quiz_correct_n': correct, 'quiz_item_n': item_n, 'quiz_missing_n': missing,
                # 任一題未作答 → 該概念 quiz_score 為 null，不納入比較
                'quiz_score': None if missing > 0 or item_n == 0 else round(correct / item_n, 4),
                'teacher_predicted': int(cid in predicted), 'on_card': int(cid in on_card),
            })
            out.append(row)
    return out
