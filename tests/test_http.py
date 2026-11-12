"""驗收測試：透過 HTTP 從空白開始，走完「教師預測 → 學生完成單元 → 驗證小考 → 教師確認 → 洞察卡 → 教師回應」，
並確認洞察卡數字可以追溯到事件紀錄。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from learntrace import server
from learntrace.content import PRACTICE, QUIZ

os.environ['LT_QUIET'] = '1'


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, who=''):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', 'X-Demo-Account': who})
        try:
            with urllib.request.urlopen(req) as res:
                raw = res.read()
                ctype = res.headers.get('Content-Type', '')
                return res.status, (json.loads(raw) if 'json' in ctype else raw.decode('utf-8-sig'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class AcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.httpd = server.serve(tempfile.mkdtemp(), port=0, scenario='blank')
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.c = Client(f'http://127.0.0.1:{cls.httpd.server_address[1]}')

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def ok(self, method, path, body=None, who=''):
        status, data = self.c.call(method, path, body, who)
        self.assertEqual(status, 200, (path, data))
        return data

    def test_full_flow_from_blank(self):
        T, RS = 'T01', 'RS01'
        # 教師：事前預測（送出後開放學習）、鎖定小考題目
        self.ok('POST', '/api/teacher/predictions',
                {'predictions': [{'concept_id': 'c2', 'reason': '曲線容易混淆'}, {'concept_id': 'c3'}]}, T)
        status, _ = self.c.call('POST', '/api/teacher/predictions', {'predictions': [{'concept_id': 'c1'}, {'concept_id': 'c2'}]}, T)
        self.assertEqual(status, 409)
        self.ok('POST', '/api/teacher/quiz_items/lock', None, T)

        # 10 位學生同意研究並完成單元；其中 6 人第一次答錯 c1，ST11 不同意研究
        students = [f'ST{i:02d}' for i in range(1, 12)]
        for i, sid in enumerate(students):
            self.ok('POST', '/api/student/consent', {'decision': 'declined' if sid == 'ST11' else 'consented'}, sid)
            for cid, p in PRACTICE.items():
                self.ok('POST', '/api/student/view', {'concept_id': cid, 'level': 1}, sid)
                if cid == 'c1' and (i < 6 or sid == 'ST11'):
                    self.ok('POST', '/api/student/practice', {'item_id': p['item_id'], 'choice': '變小'}, sid)
                self.ok('POST', '/api/student/practice', {'item_id': p['item_id'], 'choice': p['answer']}, sid)
            if i < 2:
                self.ok('POST', '/api/student/level_down', {'concept_id': 'c1'}, sid)
            if sid == 'ST11':   # 不同意研究者：按鈕照常可用，但不記錄、不保存
                nu = self.ok('POST', '/api/student/not_understood', {'concept_id': 'c1'}, sid)
                q = self.ok('POST', '/api/student/question', {'concept_id': 'c1', 'text': '還是不懂'}, sid)
                self.assertEqual((nu['recorded'], q['recorded']), (False, False))
            r = self.ok('POST', '/api/student/complete', None, sid)
            self.assertTrue(r['completed'])
        # 學生端不能做教師的事
        status, _ = self.c.call('POST', '/api/teacher/open_quiz', None, 'ST01')
        self.assertEqual(status, 403)

        # ST12 同意研究但沒完成單元
        self.ok('POST', '/api/student/consent', {'decision': 'consented'}, 'ST12')
        self.ok('POST', '/api/student/view', {'concept_id': 'c1', 'level': 1}, 'ST12')
        self.ok('POST', '/api/teacher/open_quiz', None, T)
        status, _ = self.c.call('POST', '/api/student/quiz/start', None, 'ST12')
        self.assertEqual(status, 409)
        for sid in students:
            q = self.ok('POST', '/api/student/quiz/start', None, sid)
            self.assertNotIn('answer', json.dumps(q['items'], ensure_ascii=False).replace('"options"', ''))
            answers = {f'q-{cid}-{k + 1}': item['answer'] for cid, items in QUIZ.items() for k, item in enumerate(items)}
            self.ok('POST', '/api/student/quiz/submit', {'answers': answers}, sid)

        status = self.ok('GET', '/api/teacher/quiz_status', None, T)
        self.assertEqual((status['consented_n'], status['completed_n']), (11, 10))
        bad, _ = self.c.call('POST', '/api/teacher/quiz_confirm', {'map_ok': True, 'closed_ok': False}, T)
        self.assertEqual(bad, 400)
        self.assertEqual(self.ok('POST', '/api/teacher/quiz_confirm', {'map_ok': True, 'closed_ok': True}, T)['cards'], 1)

        cards = self.ok('GET', '/api/teacher/cards', None, T)['cards']
        self.assertEqual(len(cards), 1)
        sig = {s['signal']: s for s in cards[0]['display']['signals']}
        self.assertEqual(sig['practice_first_wrong']['text'], '6 / 10 人（60.0%）')
        self.assertTrue(sig['level_down']['masked'])
        self.assertEqual(cards[0]['display']['denominator_text'], '本輪可分析學生 10 / 11 人')

        card_id = cards[0]['card_id']
        self.ok('POST', f'/api/teacher/cards/{card_id}/response', {'response': 'adopt', 'note': '下週補充生活案例'}, T)
        again, _ = self.c.call('POST', f'/api/teacher/cards/{card_id}/response', {'response': 'later', 'note': 'x'}, T)
        self.assertEqual(again, 409)
        cards = self.ok('GET', '/api/teacher/cards', None, T)['cards']
        self.assertEqual(cards[0]['response']['label'], '採納')

        # 研究者：追溯與匯出
        trace = self.ok('GET', f'/api/research/trace/{card_id}', None, RS)
        self.assertTrue(trace['all_match'])
        wrong = next(s for s in trace['signals'] if s['signal'] == 'practice_first_wrong')
        self.assertEqual(len(wrong['evidence']), 6)
        status, csv_text = self.c.call('GET', '/api/research/export.csv', None, RS)
        self.assertEqual(status, 200)
        self.assertEqual(len(csv_text.strip().splitlines()), 1 + 10 * 3)
        status, _ = self.c.call('GET', f'/api/research/trace/{card_id}', None, T)
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()
