"""LearnTrace MVP 伺服器（只用 Python 標準函式庫）。

    python3 -m learntrace.server --reset A      # 情境 A，開在 http://127.0.0.1:8000

假資料版沒有登入：前端用 X-Demo-Account 標頭指定「現在扮演誰」，伺服器再依帳號角色檢查權限。
正式版必須換成學校登入，不能沿用這個做法。
"""
import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import seed
from . import services as S
from .db import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, 'web')
R = seed.ROUND_ID


class App:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.conn = connect(data_dir)

    def reset(self, scenario):
        self.conn.close()
        self.conn = seed.reset(self.data_dir, scenario)


def routes():
    """(方法, 路徑樣式, 處理函式)。處理函式拿到 (conn, who, body, *路徑參數)。"""
    j = lambda b, k, d=None: (b or {}).get(k, d)
    return [
        # 學生
        ('GET', r'/api/student/state', lambda c, w, b: S.student_state(c, w, R)),
        ('POST', r'/api/student/consent', lambda c, w, b: S.set_consent(c, w, R, j(b, 'decision'))),
        ('GET', r'/api/student/unit', lambda c, w, b: S.unit_material(c, w, R)),
        ('POST', r'/api/student/view', lambda c, w, b: S.view_content(c, w, R, j(b, 'concept_id'), j(b, 'level'))),
        ('POST', r'/api/student/practice', lambda c, w, b: S.answer_practice(c, w, R, j(b, 'item_id'), j(b, 'choice'))),
        ('POST', r'/api/student/level_down', lambda c, w, b: S.level_down(c, w, R, j(b, 'concept_id'))),
        ('POST', r'/api/student/not_understood', lambda c, w, b: S.not_understood(c, w, R, j(b, 'concept_id'))),
        ('POST', r'/api/student/question', lambda c, w, b: S.submit_question(c, w, R, j(b, 'concept_id'), j(b, 'text'))),
        ('POST', r'/api/student/complete', lambda c, w, b: S.request_unit_complete(c, w, R)),
        ('GET', r'/api/student/quiz', lambda c, w, b: S.quiz_view(c, w, R)),
        ('POST', r'/api/student/quiz/start', lambda c, w, b: S.quiz_start(c, w, R)),
        ('POST', r'/api/student/quiz/submit', lambda c, w, b: S.quiz_submit(c, w, R, j(b, 'answers', {}))),
        # 教師
        ('GET', r'/api/teacher/overview', lambda c, w, b: S.teacher_overview(c, w, R)),
        ('GET', r'/api/teacher/concepts', lambda c, w, b: S.teacher_concepts(c, w, R)),
        ('GET', r'/api/teacher/predictions', lambda c, w, b: S.get_predictions(c, w, R)),
        ('POST', r'/api/teacher/predictions', lambda c, w, b: S.submit_predictions(c, w, R, j(b, 'predictions'))),
        ('GET', r'/api/teacher/quiz_items', lambda c, w, b: S.teacher_quiz_items(c, w, R)),
        ('POST', r'/api/teacher/quiz_items/lock', lambda c, w, b: S.lock_quiz_items(c, w, R)),
        ('POST', r'/api/teacher/quiz_items/([\w-]+)',
         lambda c, w, b, qid: S.update_quiz_item(c, w, qid, j(b, 'stem'), j(b, 'options'), j(b, 'answer'))),
        ('POST', r'/api/teacher/open_quiz', lambda c, w, b: S.open_quiz(c, w, R)),
        ('GET', r'/api/teacher/quiz_status', lambda c, w, b: S.quiz_status(c, w, R)),
        ('POST', r'/api/teacher/quiz_confirm',
         lambda c, w, b: S.confirm_quiz(c, w, R, j(b, 'map_ok'), j(b, 'closed_ok'))),
        ('GET', r'/api/teacher/cards', lambda c, w, b: S.teacher_cards(c, w, R)),
        ('POST', r'/api/teacher/cards/([\w-]+)/response',
         lambda c, w, b, cid: S.respond_card(c, w, cid, j(b, 'response'), j(b, 'note'))),
        ('POST', r'/api/teacher/cards/([\w-]+)/note', lambda c, w, b, cid: S.add_action_note(c, w, cid, j(b, 'note'))),
        # 研究者
        ('GET', r'/api/research/cards', lambda c, w, b: S.research_cards(c, w, R)),
        ('GET', r'/api/research/trace/([\w-]+)', lambda c, w, b, cid: S.trace_card(c, w, cid)),
    ]


ROUTES = [(m, re.compile('^' + p + '$'), f) for m, p, f in routes()]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LearnTraceMVP/0.1'

        def log_message(self, fmt, *args):
            if os.environ.get('LT_QUIET') != '1':
                super().log_message(fmt, *args)

        def _send(self, status, payload, ctype='application/json; charset=utf-8', extra=None):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                raise S.LTError(400, '請求格式錯誤')

        def do_GET(self):
            self._dispatch('GET')

        def do_POST(self):
            self._dispatch('POST')

        def _dispatch(self, method):
            path = urlparse(self.path).path
            try:
                if path.startswith('/api/'):
                    return self._api(method, path)
                if method == 'GET':
                    return self._static(path)
                self._send(404, {'error': '找不到'})
            except S.LTError as e:
                self._send(e.status, {'error': e.message})
            except sqlite3.IntegrityError as e:
                # 資料庫端的鎖定與唯一限制擋下的寫入
                self._send(409, {'error': '資料已鎖定或重複，資料庫拒絕這次寫入', 'detail': str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(500, {'error': '伺服器錯誤', 'detail': repr(e)})

        def _api(self, method, path):
            body = self._body() if method == 'POST' else {}
            with app.lock:
                conn = app.conn
                if path == '/api/demo/accounts' and method == 'GET':
                    out = [dict(r) for r in conn.execute(
                        'SELECT a.account_id, a.role, a.display_name, rc.consent FROM ident.accounts a '
                        'LEFT JOIN ident.research_codes rc ON rc.account_id = a.account_id ORDER BY a.account_id')]
                    stage = conn.execute('SELECT stage FROM rounds WHERE round_id=?', (R,)).fetchone()
                    return self._send(200, {'accounts': out, 'stage': stage['stage'] if stage else None,
                                            'stage_label': S.stage_label(stage['stage']) if stage else None})
                if path == '/api/dev/reset' and method == 'POST':
                    app.reset(body.get('scenario', 'blank'))
                    return self._send(200, {'ok': True})
                if path == '/api/research/export.csv' and method == 'GET':
                    who = self.headers.get('X-Demo-Account') or ''
                    data = S.export_rows(conn, who, R)
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=S.EXPORT_FIELDS)
                    w.writeheader()
                    for row in data:
                        w.writerow({k: ('' if v is None else v) for k, v in row.items()})
                    return self._send(200, ('﻿' + buf.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8',
                                      {'Content-Disposition': 'attachment; filename="learntrace_rq1_export.csv"'})
                who = self.headers.get('X-Demo-Account') or ''
                for m, rx, fn in ROUTES:
                    if m != method:
                        continue
                    mt = rx.match(path)
                    if mt:
                        return self._send(200, fn(conn, who, body, *mt.groups()))
            self._send(404, {'error': '找不到這個 API'})

        def _static(self, path):
            if path in ('', '/'):
                path = '/index.html'
            full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip('/')))
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                return self._send(404, {'error': '找不到'})
            ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
            if ctype.startswith('text/') or ctype in ('application/javascript',):
                ctype += '; charset=utf-8'
            with open(full, 'rb') as f:
                self._send(200, f.read(), ctype)

    return Handler


def serve(data_dir, host='127.0.0.1', port=8000, scenario=None):
    app = App(data_dir)
    if scenario:
        app.reset(scenario)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return app, httpd


def main():
    ap = argparse.ArgumentParser(description='LearnTrace MVP（假資料版）')
    ap.add_argument('--data', default=os.path.join(ROOT, 'data'))
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--reset', choices=['blank', 'A', 'B'], help='啟動時重設資料')
    a = ap.parse_args()
    if not a.reset and not os.path.exists(os.path.join(a.data, 'research.db')):
        a.reset = 'A'
    _, httpd = serve(a.data, a.host, a.port, a.reset)
    print(f'LearnTrace MVP：http://{a.host}:{a.port}  （假資料版，Ctrl+C 結束）')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
