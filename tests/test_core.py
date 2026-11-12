"""業務規則與資料庫鎖定的測試。執行：python3 -m unittest discover -s tests -v"""
import json
import sqlite3
import tempfile
import unittest

from learntrace import seed, services as S
from learntrace.content import PRACTICE, QUIZ

R, T, RS = seed.ROUND_ID, seed.TEACHER_ID, seed.RESEARCHER_ID


_OPEN = []


def fresh(scenario='blank'):
    conn = seed.reset(tempfile.mkdtemp(), scenario)
    _OPEN.append(conn)
    return conn


def tearDownModule():
    for conn in _OPEN:
        conn.close()


def code_of(conn, sid):
    return conn.execute('SELECT research_code FROM ident.research_codes WHERE account_id=?', (sid,)).fetchone()[0]


def count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def open_learning(conn):
    S.submit_predictions(conn, T, R, [{'concept_id': 'c1'}, {'concept_id': 'c2'}])


class ConsentTest(unittest.TestCase):
    def test_declined_student_leaves_nothing_in_research_tables(self):
        c = fresh()
        open_learning(c)
        sid = 'ST01'
        S.set_consent(c, sid, R, 'declined')
        code = code_of(c, sid)
        p = PRACTICE['c1']
        S.view_content(c, sid, R, 'c1', 1)
        S.answer_practice(c, sid, R, p['item_id'], '變小')
        S.level_down(c, sid, R, 'c1')
        with self.assertRaises(S.LTError):
            S.not_understood(c, sid, R, 'c1')
        with self.assertRaises(S.LTError):
            S.submit_question(c, sid, R, 'c1', '問題')
        for t in ('event_log', 'signal_events', 'questions', 'quiz_attempts', 'quiz_responses'):
            self.assertEqual(count(c, f'SELECT COUNT(*) FROM {t} WHERE research_code=?', (code,)), 0, t)
        # 營運層照常記錄進度
        prog = c.execute("SELECT * FROM ops.app_progress WHERE account_id=? AND concept_id='c1'", (sid,)).fetchone()
        self.assertIsNotNone(prog['level1_viewed_at'])
        self.assertEqual(prog['current_level'], 2)

    def test_declined_quiz_answers_are_discarded(self):
        c = fresh()
        open_learning(c)
        S.lock_quiz_items(c, T, R)
        S.open_quiz(c, T, R)
        S.set_consent(c, 'ST02', R, 'declined')
        S.quiz_start(c, 'ST02', R)
        S.quiz_submit(c, 'ST02', R, {'q-c1-1': 'A'})
        self.assertEqual(count(c, 'SELECT COUNT(*) FROM quiz_responses'), 0)
        self.assertEqual(count(c, 'SELECT COUNT(*) FROM quiz_attempts'), 0)
        u = c.execute("SELECT quiz_completed_at FROM ops.app_unit WHERE account_id='ST02'").fetchone()
        self.assertIsNotNone(u[0])

    def test_consent_choice_is_once(self):
        c = fresh()
        S.set_consent(c, 'ST03', R, 'consented')
        with self.assertRaises(S.LTError):
            S.set_consent(c, 'ST03', R, 'declined')

    def test_must_decide_consent_before_learning(self):
        c = fresh()
        open_learning(c)
        with self.assertRaises(S.LTError):
            S.view_content(c, 'ST04', R, 'c1', 1)


class SignalTest(unittest.TestCase):
    def setUp(self):
        self.c = fresh()
        open_learning(self.c)
        S.set_consent(self.c, 'ST01', R, 'consented')
        self.code = code_of(self.c, 'ST01')

    def signals(self, kind):
        return count(self.c, 'SELECT COUNT(*) FROM signal_events WHERE research_code=? AND signal_type=?',
                     (self.code, kind))

    def test_repeat_clicks_count_once_but_all_are_logged(self):
        for _ in range(4):
            S.not_understood(self.c, 'ST01', R, 'c1')
        self.assertEqual(self.signals('not_understood'), 1)
        self.assertEqual(count(self.c, "SELECT COUNT(*) FROM event_log WHERE event_type='not_understood'"), 4)

    def test_level_down_twice_is_one_signal(self):
        S.level_down(self.c, 'ST01', R, 'c1')
        S.level_down(self.c, 'ST01', R, 'c1')
        with self.assertRaises(S.LTError):
            S.level_down(self.c, 'ST01', R, 'c1')       # 已是第 3 層
        self.assertEqual(self.signals('level_down'), 1)

    def test_quiz_wrong_only_on_first_attempt(self):
        p = PRACTICE['c1']
        S.answer_practice(self.c, 'ST01', R, p['item_id'], '變大')   # 第一次答對
        S.answer_practice(self.c, 'ST01', R, p['item_id'], '變小')   # 之後答錯
        self.assertEqual(self.signals('quiz_wrong'), 0)
        S.set_consent(self.c, 'ST02', R, 'consented')
        S.answer_practice(self.c, 'ST02', R, p['item_id'], '變小')   # 第一次答錯
        S.answer_practice(self.c, 'ST02', R, p['item_id'], '變大')
        self.assertEqual(count(self.c, "SELECT COUNT(*) FROM signal_events WHERE signal_type='quiz_wrong'"), 1)

    def test_correctness_is_judged_by_server(self):
        with self.assertRaises(S.LTError):
            S.answer_practice(self.c, 'ST01', R, PRACTICE['c1']['item_id'], '不是選項')

    def test_question_gets_server_id_and_redaction(self):
        S.submit_question(self.c, 'ST01', R, 'c1', '我是 115AC8020，email a@b.com，電話 0912345678')
        q = self.c.execute('SELECT * FROM questions').fetchone()
        self.assertTrue(q['question_id'].startswith('Q-'))
        self.assertEqual(q['pii_flag'], 1)
        for leak in ('115AC8020', 'a@b.com', '0912345678'):
            self.assertNotIn(leak, q['text_redacted'])
        self.assertEqual(self.signals('question_submitted'), 1)

    def test_unit_completed_requires_all_conditions(self):
        r = S.request_unit_complete(self.c, 'ST01', R)
        self.assertFalse(r['completed'])
        self.assertEqual(len(r['missing']), 6)
        for cid, p in PRACTICE.items():
            S.view_content(self.c, 'ST01', R, cid, 1)
            S.answer_practice(self.c, 'ST01', R, p['item_id'], p['answer'])
        self.assertTrue(S.request_unit_complete(self.c, 'ST01', R)['completed'])
        S.request_unit_complete(self.c, 'ST01', R)       # 重按不會多記
        self.assertEqual(count(self.c, "SELECT COUNT(*) FROM event_log WHERE event_type='unit_completed'"), 1)

    def test_cannot_jump_to_simpler_level_without_level_down(self):
        with self.assertRaises(S.LTError):
            S.view_content(self.c, 'ST01', R, 'c1', 3)


class LockTest(unittest.TestCase):
    def test_database_rejects_changes_to_locked_data(self):
        c = fresh('A')
        stmts = [
            "UPDATE teacher_predictions SET concept_id='c1'",
            'DELETE FROM teacher_predictions',
            "UPDATE event_log SET event_type='level_down'",
            'DELETE FROM signal_events',
            "UPDATE quiz_responses SET correct=1",
            "UPDATE unit_content SET body='x'",
            "UPDATE practice_items SET answer='x'",
            "UPDATE quiz_items SET answer='x'",
            "UPDATE concepts SET name='x'",
            "UPDATE rounds SET stage='prediction'",
            "UPDATE quiz_attempts SET status='started' WHERE status='submitted'",
        ]
        for sql in stmts:
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                c.execute(sql)

    def test_predictions_once_two_to_three_distinct(self):
        c = fresh()
        for bad in ([{'concept_id': 'c1'}], [{'concept_id': 'c1'}, {'concept_id': 'c1'}],
                    [{'concept_id': 'c1'}, {'concept_id': 'c2'}, {'concept_id': 'c3'}, {'concept_id': 'c1'}],
                    [{'concept_id': 'c1'}, {'concept_id': 'zz'}]):
            with self.assertRaises(S.LTError):
                S.submit_predictions(c, T, R, bad)
        S.submit_predictions(c, T, R, [{'concept_id': 'c1', 'reason': '公式背得但不懂'}, {'concept_id': 'c3'}])
        self.assertEqual(c.execute('SELECT stage FROM rounds').fetchone()[0], 'learning')
        with self.assertRaises(S.LTError):
            S.submit_predictions(c, T, R, [{'concept_id': 'c2'}, {'concept_id': 'c3'}])

    def test_quiz_needs_locked_items_and_two_per_concept(self):
        c = fresh()
        open_learning(c)
        with self.assertRaises(S.LTError):
            S.open_quiz(c, T, R)
        S.lock_quiz_items(c, T, R)
        with self.assertRaises(S.LTError):
            S.update_quiz_item(c, T, 'q-c1-1', 'x', ['a', 'b', 'c', 'd'], 'a')

    def test_card_response_once_and_note_required(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        cid = 'card-R1-c1'
        with self.assertRaises(S.LTError):
            S.respond_card(c, T, cid, 'adopt', '  ')
        S.respond_card(c, T, cid, 'adopt', '下週補充生活案例')
        with self.assertRaises(S.LTError):
            S.respond_card(c, T, cid, 'disagree', '改主意')
        S.add_action_note(c, T, cid, '已在課堂補講 5 分鐘')
        self.assertEqual(count(c, 'SELECT COUNT(*) FROM teacher_action_notes'), 1)

    def test_roles_are_checked(self):
        c = fresh('A')
        with self.assertRaises(S.LTError):
            S.quiz_status(c, 'ST01', R)
        with self.assertRaises(S.LTError):
            S.export_rows(c, T, R)


class QuizTest(unittest.TestCase):
    def test_attempt_lifecycle_and_expiry(self):
        c = fresh('A')
        status = S.quiz_status(c, T, R)
        self.assertEqual((status['consented_n'], status['completed_n'], status['not_completed_n'],
                          status['missing_cells']), (28, 26, 2, 1))
        S.confirm_quiz(c, T, R, True, True)
        rows = dict(c.execute('SELECT status, COUNT(*) FROM quiz_attempts GROUP BY status').fetchall())
        self.assertEqual(rows, {'submitted': 26, 'expired': 1})

    def test_submit_once(self):
        c = fresh()
        open_learning(c)
        S.lock_quiz_items(c, T, R)
        S.open_quiz(c, T, R)
        S.set_consent(c, 'ST01', R, 'consented')
        with self.assertRaises(S.LTError):
            S.quiz_submit(c, 'ST01', R, {})          # 要先開始
        S.quiz_start(c, 'ST01', R)
        S.quiz_submit(c, 'ST01', R, {'q-c1-1': 'B'})
        with self.assertRaises(S.LTError):
            S.quiz_submit(c, 'ST01', R, {'q-c1-1': 'A'})
        self.assertEqual(count(c, 'SELECT COUNT(*) FROM quiz_responses WHERE choice IS NULL'), 5)

    def test_items_sent_to_students_have_no_answers_or_concepts(self):
        c = fresh('A')
        v = S.quiz_view(c, 'ST27', R)   # idx 26 沒開始作答
        self.assertEqual(len(v['items']), 6)
        for it in v['items']:
            self.assertEqual(set(it), {'quiz_item_id', 'stem', 'options'})

    def test_score_is_null_when_any_item_missing(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        rows = S.export_rows(c, RS, R)
        code24 = code_of(c, seed.student_id(24))
        r = next(x for x in rows if x['research_code'] == code24 and x['concept_id'] == 'c3')
        self.assertEqual((r['quiz_correct_n'], r['quiz_item_n'], r['quiz_missing_n'], r['quiz_score']), (1, 2, 1, None))
        code0 = code_of(c, seed.student_id(0))
        r0 = next(x for x in rows if x['research_code'] == code0 and x['concept_id'] == 'c1')
        self.assertEqual((r0['quiz_correct_n'], r0['quiz_score']), (0, 0.0))
        r20 = next(x for x in rows if x['research_code'] == code_of(c, seed.student_id(20)) and x['concept_id'] == 'c1')
        self.assertEqual(r20['quiz_score'], 1.0)


class CardTest(unittest.TestCase):
    def test_threshold_rounds_up(self):
        self.assertEqual(S.card_threshold(27), 9)
        self.assertEqual(S.card_threshold(25), 8)
        self.assertEqual(S.card_threshold(10), 5)
        self.assertEqual(S.card_threshold(30), 9)

    def test_scenario_a_makes_one_card_with_masked_small_counts(self):
        c = fresh('A')
        self.assertEqual(S.confirm_quiz(c, T, R, True, True)['cards'], 1)
        card = c.execute('SELECT * FROM insight_cards').fetchone()
        self.assertEqual(card['concept_id'], 'c1')
        self.assertEqual((card['analyzable_n'], card['consented_n']), (25, 28))
        self.assertEqual(json.loads(card['counts']),
                         {'quiz_wrong': 12, 'level_down': 8, 'not_understood': 7, 'question_submitted': 4})
        disp = json.loads(card['display'])
        q = next(s for s in disp['signals'] if s['signal'] == 'question_submitted')
        self.assertEqual((q['masked'], q['text']), (True, '少於 5 人'))
        self.assertNotIn('4', json.dumps(q, ensure_ascii=False))
        self.assertEqual(disp['denominator_text'], '本輪可分析學生 25 / 28 人')
        self.assertIsNone(card['confusion_summary'])          # 只有 4 位提問者
        # 7 人有訊號但未達 30%（需 8 人）的概念不出卡
        self.assertIsNone(c.execute("SELECT 1 FROM insight_cards WHERE concept_id='c2'").fetchone())

    def test_scenario_b_makes_no_card(self):
        c = fresh('B')
        self.assertEqual(S.confirm_quiz(c, T, R, True, True)['cards'], 0)
        self.assertEqual(S.teacher_cards(c, T, R)['cards'], [])

    def test_teacher_view_has_no_exact_small_counts_or_codes(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        text = json.dumps(S.teacher_cards(c, T, R), ensure_ascii=False)
        self.assertNotIn('S-', text)
        self.assertNotIn('"counts"', text)

    def test_card_numbers_trace_back_to_event_log(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        t = S.trace_card(c, RS, 'card-R1-c1')
        self.assertTrue(t['all_match'])
        for s in t['signals']:
            self.assertEqual(s['card_count'], len(s['evidence']))
            for e in s['evidence']:
                row = c.execute('SELECT research_code, concept_id FROM event_log WHERE log_id=?', (e['log_id'],)).fetchone()
                self.assertEqual((row['research_code'], row['concept_id']), (e['research_code'], 'c1'))

    def test_card_is_a_snapshot(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        with self.assertRaises(S.LTError):
            S.not_understood(c, 'ST01', R, 'c1')     # 學習已關閉，不能再加訊號
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE insight_cards SET display='{}'")

    def test_export_only_analyzable_students(self):
        c = fresh('A')
        S.confirm_quiz(c, T, R, True, True)
        rows = S.export_rows(c, RS, R)
        self.assertEqual(len(rows), 25 * 3)
        excluded = {code_of(c, seed.student_id(i)) for i in range(25, 30)}
        self.assertFalse(excluded & {r['research_code'] for r in rows})
        r = rows[0]
        self.assertEqual(set(r), set(S.EXPORT_FIELDS))


if __name__ == '__main__':
    unittest.main()
