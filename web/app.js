'use strict';
// LearnTrace MVP 前端（假資料版）。沒有登入：網址決定「現在扮演誰」，伺服器依帳號角色檢查權限。

const $app = document.getElementById('app');
const TEACHER = 'T01';
const RESEARCHER = 'RS01';
const SIGNAL_LABELS = { quiz_wrong: '答錯', level_down: '要求簡化說明', not_understood: '按「聽不懂」', question_submitted: '留下相關問題' };
const RESPONSES = [['adopt', '採納'], ['later', '延後'], ['known', '早就知道'], ['disagree', '不同意']];
const RESPONSE_EXAMPLE = {
  adopt: '例如：下週上課先用充電例子講 5 分鐘',
  later: '例如：期中考後再補，這週進度太趕',
  known: '例如：這部分下一單元本來就會再講',
  disagree: '例如：學生答錯是題目敘述不清，不是不懂概念',
};

// ── 小工具 ───────────────────────────────────────────
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'value') el.value = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body, who) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Demo-Account': who || '' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `錯誤 ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

async function run(fn) {
  try { await fn(); } catch (e) { toast(e.message); }
}

function setWho(text) { document.getElementById('who').textContent = text || ''; }
function show(...nodes) { $app.replaceChildren(...nodes.flat().filter(Boolean)); }
function steps(labels, nowIndex) {
  return h('ol', { class: 'steps' }, labels.map((l, i) =>
    h('li', { class: i === nowIndex ? 'now' : i < nowIndex ? 'done' : '' },
      h('b', {}, `${i + 1} ${l}`), i < nowIndex ? '已完成' : i === nowIndex ? '進行中' : '尚未開始')));
}

// ── 路由 ─────────────────────────────────────────────
async function render() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  try {
    if (parts[0] === 'student' && parts[1]) await renderStudent(parts[1]);
    else if (parts[0] === 'teacher') await renderTeacher();
    else if (parts[0] === 'research') await renderResearch();
    else await renderHome();
  } catch (e) {
    show(h('div', { class: 'card' }, h('h2', {}, '無法載入'), h('p', {}, e.message), h('a', { href: '#/' }, '回首頁')));
  }
}
window.addEventListener('hashchange', render);

// ── 首頁：情境與角色 ────────────────────────────────────
async function renderHome() {
  setWho('');
  const d = await api('GET', '/api/demo/accounts');
  const students = d.accounts.filter((a) => a.role === 'student');
  const consentText = { pending: '尚未選擇', consented: '同意研究', declined: '不參與研究', withdrawn: '已撤回' };
  const reset = (scenario) => run(async () => {
    await api('POST', '/api/dev/reset', { scenario });
    toast('資料已重設');
    render();
  });
  show(
    h('div', { class: 'notice' }, '假資料版：所有學生都是虛構的，不串外部 AI，不收真人資料。沒有登入，下方按鈕決定你現在扮演誰。'),
    h('section', { class: 'card' },
      h('h1', {}, 'RC 電路：時間常數｜第 1 輪'),
      h('p', {}, '目前階段：', h('b', {}, d.stage_label || '—')),
      h('h2', {}, '重設測試情境'),
      h('div', { class: 'grid3' },
        h('button', { class: 'btn', onclick: () => reset('A') }, '情境 A：多人卡關'),
        h('button', { class: 'btn', onclick: () => reset('B') }, '情境 B：沒有明顯卡關'),
        h('button', { class: 'btn', onclick: () => reset('blank') }, '空白：從事前預測開始')),
      h('p', { class: 'muted' }, '情境 A、B 已模擬 30 位學生完成學習與驗證小考，停在「驗證小考進行中」，接著由教師確認小考資料、查看洞察卡、送出回應。')),
    h('section', { class: 'card' },
      h('h2', {}, '以誰的身分進入'),
      h('div', { class: 'grid3' },
        h('a', { class: 'btn primary', href: '#/teacher', style: 'display:flex;align-items:center;justify-content:center;text-decoration:none' }, '教師'),
        h('a', { class: 'btn', href: '#/research', style: 'display:flex;align-items:center;justify-content:center;text-decoration:none' }, '研究者（追溯與匯出）')),
      h('h3', {}, '學生'),
      h('div', { class: 'students' }, students.map((s) =>
        h('a', { class: 'btn', href: `#/student/${s.account_id}`, style: 'display:flex;flex-direction:column;justify-content:center;text-decoration:none;padding:6px 10px;line-height:1.3' },
          h('span', {}, s.display_name), h('span', { class: 'muted' }, consentText[s.consent] || ''))))),
  );
}

// ── 學生端 ───────────────────────────────────────────
const studentUI = {};

async function renderStudent(id) {
  const ui = studentUI[id] || (studentUI[id] = { idx: 0, picks: {}, feedback: {}, nuDone: {}, askOpen: false, askSent: {}, missing: [], quiz: {}, confirming: false });
  const st = await api('GET', '/api/student/state', null, id);
  setWho(`學生｜${id}`);
  const back = h('a', { href: '#/' }, '← 回首頁');

  if (st.consent === 'pending') return show(back, consentView(id));
  const banner = st.consent === 'declined'
    ? h('div', { class: 'info' }, '你選擇不參與研究：系統照常使用，但不會記錄你的學習訊號，也不保存提問與小考作答。')
    : null;

  if (st.stage === 'prediction') return show(back, banner, h('div', { class: 'card' }, h('h2', {}, '老師尚未開放本單元'), h('p', {}, '請稍後再回來。')));
  if (st.stage === 'learning') return show(back, banner, await unitView(id, st, ui));
  if (st.stage === 'quiz') return show(back, banner, await quizView(id, ui));
  return show(back, banner, h('div', { class: 'card' },
    h('h2', {}, st.quiz_completed ? '已完成單元小考，謝謝你。' : '本輪驗證小考已截止'),
    h('p', {}, st.quiz_completed ? '本測驗不計入課程成績，系統不顯示分數與正確答案。' : '本測驗不計入課程成績。')));
}

function consentView(id) {
  const decide = (decision) => run(async () => {
    await api('POST', '/api/student/consent', { decision }, id);
    render();
  });
  return h('section', { class: 'card' },
    h('div', { class: 'muted' }, '研究參與說明（示範版，正式版以倫理審查核准的同意書為準）'),
    h('h1', {}, '要不要讓你的匿名學習資料用於研究？'),
    h('p', {}, '同意的話，系統會用代號記錄你在本單元的答錯、看簡化說明、按「我還是不懂」、提問與驗證小考作答，用來研究老師能否提早看見全班卡關的概念。老師只看得到全班統計，看不到你的名字或個人資料。'),
    h('p', {}, '不參與也可以照常使用系統，不影響成績；你的學習訊號、提問與小考作答都不會被保存。'),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: () => decide('consented') }, '同意參與研究'),
      h('button', { class: 'btn', onclick: () => decide('declined') }, '不參與研究')));
}

async function unitView(id, st, ui) {
  const unit = await api('GET', '/api/student/unit', null, id);
  const concepts = unit.concepts;
  const consented = st.consent === 'consented';
  if (st.unit_completed) {
    return h('section', { class: 'card' }, h('h2', {}, '已完成本單元'), h('p', {}, '老師開放驗證小考後，回到這裡作答。'));
  }
  const c = concepts[ui.idx];
  const prog = st.progress[c.concept_id];
  const level = prog.current_level;
  const lv = c.levels[level - 1];

  // 第一次進到這個概念的第 1 層：記錄瀏覽
  if (level === 1 && !prog.level1_viewed) {
    await api('POST', '/api/student/view', { concept_id: c.concept_id, level: 1 }, id);
    prog.level1_viewed = true;
  }

  const go = (i) => run(async () => {
    ui.idx = i; ui.askOpen = false; ui.missing = [];
    const next = concepts[i];
    if (st.progress[next.concept_id].current_level === 1) {
      await api('POST', '/api/student/view', { concept_id: next.concept_id, level: 1 }, id);
    }
    render();
  });

  const fb = ui.feedback[c.concept_id];
  const solved = fb && fb.correct;
  const pick = ui.picks[c.concept_id];

  const practice = h('section', { class: 'card' },
    h('div', { class: 'muted' }, '課中練習'),
    h('h2', {}, c.practice.stem),
    h('div', { role: 'radiogroup', 'aria-label': '選項', style: 'display:flex;flex-direction:column;gap:8px' },
      c.practice.options.map((o) => h('label', { class: 'choice' + (pick === o ? ' on' : '') },
        h('input', { type: 'radio', name: 'practice-' + c.concept_id, checked: pick === o, disabled: solved,
          onchange: () => { ui.picks[c.concept_id] = o; render(); } }),
        h('span', {}, o)))),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', disabled: !pick || solved, onclick: () => run(async () => {
        ui.feedback[c.concept_id] = await api('POST', '/api/student/practice', { item_id: c.practice.item_id, choice: pick }, id);
        render();
      }) }, '確認答案')),
    fb ? h('div', { class: fb.correct ? 'info' : 'notice', role: 'status' }, h('b', {}, fb.correct ? '答對了。' : '再想想。'), fb.message) : null);

  const help = h('section', { class: 'card' },
    h('h2', {}, '需要更多協助嗎？'),
    h('div', { class: consented ? 'grid3' : 'row' },
      h('button', { class: 'btn', disabled: level >= 3, onclick: () => run(async () => {
        await api('POST', '/api/student/level_down', { concept_id: c.concept_id }, id);
        render();
      }) }, level >= 3 ? '已是最簡單的說明' : '看更簡單的說明'),
      consented ? h('button', { class: 'btn' + (ui.nuDone[c.concept_id] ? ' selected' : ''), onclick: () => run(async () => {
        await api('POST', '/api/student/not_understood', { concept_id: c.concept_id }, id);
        ui.nuDone[c.concept_id] = true;
        render();
      }) }, ui.nuDone[c.concept_id] ? '已記錄：我還是不懂' : '我還是不懂') : null,
      consented ? h('button', { class: 'btn' + (ui.askOpen ? ' selected' : ''), onclick: () => { ui.askOpen = true; render(); } }, '留下問題') : null),
    ui.nuDone[c.concept_id] ? h('p', { class: 'muted', role: 'status' }, '已記錄。你可以看更簡單的說明，或留下問題。') : null,
    consented && ui.askOpen ? questionBox(id, c, ui) : null,
    consented && !ui.askOpen && ui.askSent[c.concept_id]
      ? h('div', { class: 'info row between', role: 'status' }, h('b', {}, '已記錄你的問題'),
        h('button', { class: 'btn link', onclick: () => { ui.askOpen = true; render(); } }, '再留一個問題'))
      : null);

  const isLast = ui.idx === concepts.length - 1;
  const nav = h('div', { class: 'row between' },
    h('button', { class: 'btn', disabled: ui.idx === 0, onclick: () => go(ui.idx - 1) }, '上一個概念'),
    isLast
      ? h('button', { class: 'btn primary', onclick: () => run(async () => {
        const r = await api('POST', '/api/student/complete', null, id);
        ui.missing = r.missing;
        if (r.completed) toast('已完成本單元');
        render();
      }) }, '完成本單元')
      : h('button', { class: 'btn primary', onclick: () => go(ui.idx + 1) }, '下一個概念'));

  return h('div', { style: 'display:flex;flex-direction:column;gap:16px' },
    h('div', { class: 'row between' },
      h('h1', {}, `${st.unit_name.split('：')[0]}｜${c.name}`),
      h('span', { class: 'muted' }, `本單元進度：${ui.idx + 1} / ${concepts.length}`)),
    h('section', { class: 'card' },
      h('div', { class: 'row between' }, h('h2', {}, lv.title), h('span', { class: 'muted' }, `教材 v${unit.content_version}｜教師審核後固定`)),
      h('p', { style: 'font-size:16px;line-height:1.85' }, lv.body),
      lv.example ? h('div', { class: 'quote' }, lv.example) : null,
      level > 1 ? h('button', { class: 'btn link', style: 'align-self:flex-start', onclick: () => run(async () => {
        await api('POST', '/api/student/view', { concept_id: c.concept_id, level: 1 }, id);
        render();
      }) }, '回到標準說明') : null),
    practice, help,
    ui.missing.length ? h('div', { class: 'notice', role: 'alert' }, h('b', {}, '還不能完成本單元：'), h('ul', { style: 'margin:4px 0 0;padding-left:20px' }, ui.missing.map((m) => h('li', {}, m)))) : null,
    nav);
}

function questionBox(id, c, ui) {
  const ta = h('textarea', { id: 'q-text', rows: 3, placeholder: '例如：我知道公式，但不知道 τ 變大代表什麼' });
  return h('div', { style: 'display:flex;flex-direction:column;gap:8px' },
    h('label', { for: 'q-text' }, h('b', {}, '你卡在哪裡？')),
    h('span', { class: 'small', style: 'color:var(--warn-ink);font-weight:500' }, '請勿輸入姓名、學號、聯絡方式。'),
    ta,
    h('div', { class: 'row', style: 'justify-content:flex-end' },
      h('button', { class: 'btn', onclick: () => { ui.askOpen = false; render(); } }, '取消'),
      h('button', { class: 'btn primary', onclick: () => run(async () => {
        if (!ta.value.trim()) return toast('請先寫下問題');
        await api('POST', '/api/student/question', { concept_id: c.concept_id, text: ta.value }, id);
        ui.askOpen = false; ui.askSent[c.concept_id] = true;
        render();
      }) }, '送出問題')));
}

async function quizView(id, ui) {
  const q = await api('GET', '/api/student/quiz', null, id);
  if (q.status === 'submitted') {
    return h('section', { class: 'card' }, h('h2', {}, '已完成單元小考，謝謝你。'), h('p', {}, '本測驗不計入課程成績，系統不顯示分數與正確答案。'));
  }
  if (q.status === 'expired') return h('section', { class: 'card' }, h('h2', {}, '本輪驗證小考已截止'));
  if (q.status === 'not_started' && !ui.quizStarted) {
    return h('section', { class: 'card' },
      h('div', { class: 'muted' }, '第 1 輪｜單元小考'),
      h('h1', {}, 'RC 電路：時間常數'),
      h('p', {}, '本測驗由授課教師命題。此測驗不計入課程成績，僅用於了解本單元概念理解情形。'),
      h('div', { class: 'row' }, h('span', { class: 'tag blue' }, `共 ${q.items.length} 題`), h('span', { class: 'tag blue' }, '預計 10 分鐘')),
      h('div', { class: 'notice' }, '請獨立完成，不可查看學習內容。作答期間學習內容會暫時關閉。'),
      h('button', { class: 'btn primary', style: 'align-self:flex-start', onclick: () => run(async () => {
        await api('POST', '/api/student/quiz/start', null, id);
        ui.quizStarted = true;
        render();
      }) }, '開始作答'));
  }
  const answered = q.items.filter((it) => ui.quiz[it.quiz_item_id]).length;
  return h('div', { style: 'display:flex;flex-direction:column;gap:16px' },
    h('div', { class: 'row between' }, h('h1', {}, '單元小考'), h('span', { class: 'muted' }, `已作答 ${answered} / ${q.items.length} 題`)),
    q.items.map((it, n) => h('section', { class: 'card' },
      h('div', { class: 'muted' }, `第 ${n + 1} 題`),
      h('h2', {}, it.stem),
      it.options.map((o) => h('label', { class: 'choice' + (ui.quiz[it.quiz_item_id] === o ? ' on' : '') },
        h('input', { type: 'radio', name: it.quiz_item_id, checked: ui.quiz[it.quiz_item_id] === o,
          onchange: () => { ui.quiz[it.quiz_item_id] = o; ui.confirming = false; render(); } }),
        h('span', {}, o))))),
    ui.confirming
      ? h('section', { class: 'card' },
        h('h2', {}, '確認送出'),
        h('p', {}, `已作答 ${answered} / ${q.items.length} 題。`),
        answered < q.items.length ? h('div', { class: 'notice' }, `還有 ${q.items.length - answered} 題沒作答。可以回去補，也可以直接送出，沒作答的題目會記為未作答。`) : null,
        h('p', { class: 'muted' }, '送出後不能再修改答案。'),
        h('div', { class: 'row between' },
          h('button', { class: 'btn', onclick: () => { ui.confirming = false; render(); } }, '回去檢查'),
          h('button', { class: 'btn primary', onclick: () => run(async () => {
            const answers = {};
            q.items.forEach((it) => { answers[it.quiz_item_id] = ui.quiz[it.quiz_item_id] || null; });
            await api('POST', '/api/student/quiz/submit', { answers }, id);
            render();
          }) }, '送出小考')))
      : h('div', { class: 'row', style: 'justify-content:flex-end' }, h('button', { class: 'btn primary', onclick: () => { ui.confirming = true; render(); } }, '檢查並送出')));
}

// ── 教師端 ───────────────────────────────────────────
const teacherUI = { preds: [{}, {}, {}], confirmPred: false, map_ok: false, closed_ok: false, resp: {}, notes: {} };

async function renderTeacher() {
  setWho('教師｜示範教師');
  const ov = await api('GET', '/api/teacher/overview', null, TEACHER);
  const stageIdx = { prediction: 0, learning: 1, quiz: 3, quiz_confirmed: 4, card_open: 4, closed: 4 }[ov.stage];
  const head = [
    h('a', { href: '#/' }, '← 回首頁'),
    steps(['事前預測', '學生學習', '驗證小考', '確認小考資料', '洞察卡與回應'], stageIdx),
  ];
  const sections = [];
  if (ov.stage === 'prediction') sections.push(await predictionForm());
  if (ov.predictions_submitted) sections.push(await predictionSummary());
  if (ov.stage === 'prediction' || ov.stage === 'learning') sections.push(await quizEditor(ov));
  if (ov.stage === 'learning') {
    sections.push(h('section', { class: 'card' },
      h('h2', {}, '開放驗證小考'),
      h('p', {}, '學生完成單元後，開放教師命題的驗證小考。開放後學習頁會關閉，之後的學習訊號不進本輪洞察卡。'),
      h('button', { class: 'btn primary', style: 'align-self:flex-start', disabled: !ov.quiz_items_locked, onclick: () => run(async () => {
        await api('POST', '/api/teacher/open_quiz', null, TEACHER);
        render();
      }) }, ov.quiz_items_locked ? '結束學習、開放驗證小考' : '請先鎖定小考題目')));
  }
  if (ov.stage === 'quiz') sections.push(await quizConfirm());
  if (ov.stage === 'card_open' || ov.stage === 'closed') sections.push(...await cardsView());
  show(head, sections);
}

async function predictionForm() {
  const { concepts, concept_list_version } = await api('GET', '/api/teacher/concepts', null, TEACHER);
  const p = teacherUI.preds;
  const chosen = p.map((x) => x.concept_id).filter(Boolean);
  const ready = p[0].concept_id && p[1].concept_id;
  const slot = (i) => h('div', { style: 'display:flex;flex-direction:column;gap:6px' },
    h('label', { for: 'pred-' + i }, h('b', {}, i === 2 ? '預測困難概念 3（選填）' : `預測困難概念 ${i + 1}`)),
    h('select', { id: 'pred-' + i, onchange: (e) => { p[i].concept_id = e.target.value || undefined; teacherUI.confirmPred = false; render(); } },
      h('option', { value: '' }, '從教師已確認的概念清單選擇'),
      concepts.map((c) => h('option', { value: c.concept_id, selected: p[i].concept_id === c.concept_id,
        disabled: chosen.includes(c.concept_id) && p[i].concept_id !== c.concept_id }, c.name))),
    h('label', { class: 'muted', for: 'reason-' + i }, '原因（選填）'),
    h('textarea', { id: 'reason-' + i, rows: 2, placeholder: '例如：學生常記得公式，但不理解物理意義',
      oninput: (e) => { p[i].reason = e.target.value; } }, p[i].reason || ''));
  return h('section', { class: 'card' },
    h('div', { class: 'muted' }, '第 1 輪｜教師事前預測'),
    h('h1', {}, 'RC 電路：時間常數'),
    h('p', {}, '請於學生開始本單元前，選出您預期學生最可能卡住的 2–3 個概念。'),
    slot(0), slot(1), slot(2),
    h('p', { class: 'muted' }, `概念只能從老師審核過的清單選（概念清單 v${concept_list_version}）。`),
    h('div', { class: 'notice' }, h('b', {}, '注意：'), '送出後，將無法修改本輪預測；完成單元小考後，才能查看系統洞察卡。'),
    teacherUI.confirmPred
      ? h('div', { class: 'card soft' }, h('b', {}, `確定送出這 ${chosen.length} 個預測？`), h('span', { class: 'muted' }, '送出後由伺服器記錄時間並鎖定，同時開放學生學習。'),
        h('div', { class: 'row', style: 'justify-content:flex-end' },
          h('button', { class: 'btn', onclick: () => { teacherUI.confirmPred = false; render(); } }, '返回修改'),
          h('button', { class: 'btn primary', onclick: () => run(async () => {
            const predictions = p.filter((x) => x.concept_id).map((x) => ({ concept_id: x.concept_id, reason: x.reason || '' }));
            await api('POST', '/api/teacher/predictions', { predictions }, TEACHER);
            teacherUI.confirmPred = false;
            render();
          }) }, '確定送出')))
      : h('div', { class: 'row between' }, h('span', { class: 'muted' }, ready ? `已選 ${chosen.length} 個概念，可以送出` : '至少要選前 2 個概念'),
        h('button', { class: 'btn primary', disabled: !ready, onclick: () => { teacherUI.confirmPred = true; render(); } }, '送出預測')));
}

async function predictionSummary() {
  const { predictions } = await api('GET', '/api/teacher/predictions', null, TEACHER);
  const t = predictions[0];
  return h('section', { class: 'card soft' },
    h('div', { class: 'row between' }, h('h2', {}, '事前預測（已鎖定）'),
      h('span', { class: 'muted' }, `送出時間 ${t.submitted_at}｜預測 v${t.prediction_version}｜概念清單 v${t.concept_list_version}`)),
    predictions.map((p) => h('div', { class: 'quote' }, h('b', {}, `預測 ${p.rank}　${p.name}`), h('div', { class: 'muted' }, p.reason ? '原因：' + p.reason : '未填原因'))));
}

async function quizEditor(ov) {
  const { items } = await api('GET', '/api/teacher/quiz_items', null, TEACHER);
  if (ov.quiz_items_locked) {
    return h('section', { class: 'card soft' },
      h('h2', {}, '驗證小考題目（已鎖定）'),
      h('p', { class: 'muted' }, '每個核心概念 2 題，和課中練習分開；學生作答時看不到題目對應的概念。'),
      h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '概念'), h('th', {}, '題目'), h('th', {}, '正確答案'))),
        h('tbody', {}, items.map((it) => h('tr', {}, h('td', {}, it.concept_name), h('td', {}, it.stem), h('td', {}, it.answer)))))));
  }
  const forms = items.map((it) => {
    const stem = h('textarea', { rows: 2 }, it.stem);
    const opts = it.options.map((o) => h('input', { type: 'text', value: o, 'aria-label': '選項' }));
    const ans = h('select', { 'aria-label': '正確答案' }, it.options.map((o) => h('option', { value: o, selected: o === it.answer }, o)));
    const sync = () => { const cur = ans.value; ans.replaceChildren(...opts.map((x) => h('option', { value: x.value, selected: x.value === cur }, x.value))); };
    opts.forEach((x) => x.addEventListener('change', sync));
    return h('div', { class: 'card soft' },
      h('div', { class: 'row between' }, h('b', {}, `${it.concept_name}｜第 ${it.seq} 題`)),
      stem, h('div', { class: 'grid4' }, opts),
      h('div', { class: 'row' }, h('span', { class: 'muted' }, '正確答案'), ans,
        h('button', { class: 'btn', onclick: () => run(async () => {
          await api('POST', `/api/teacher/quiz_items/${it.quiz_item_id}`, { stem: stem.value, options: opts.map((x) => x.value), answer: ans.value }, TEACHER);
          toast('已儲存');
        }) }, '儲存這題')));
  });
  return h('section', { class: 'card' },
    h('h2', {}, '驗證小考命題'),
    h('p', { class: 'muted' }, '每個核心概念 2 題，題型或情境不同，不要和課中練習太像。開放小考前要先鎖定，鎖定後不能修改。'),
    forms,
    h('button', { class: 'btn primary', style: 'align-self:flex-start', onclick: () => run(async () => {
      if (!confirm('鎖定後不能再修改題目，確定嗎？')) return;
      await api('POST', '/api/teacher/quiz_items/lock', null, TEACHER);
      render();
    }) }, '確認並鎖定小考題目'));
}

async function quizConfirm() {
  const s = await api('GET', '/api/teacher/quiz_status', null, TEACHER);
  const can = teacherUI.map_ok && teacherUI.closed_ok;
  return h('section', { class: 'card' },
    h('div', { class: 'muted' }, '第 1 輪｜驗證小考資料確認'),
    h('h1', {}, 'RC 電路：時間常數'),
    h('div', {}, h('span', { class: 'tag' }, '等待教師確認')),
    h('h2', {}, '題目與概念對應'),
    h('div', { class: 'scroll' }, h('table', {}, h('tbody', {}, s.mapping.map((m) => h('tr', {}, h('td', { class: 'muted', style: 'width:90px' }, `第 ${m.no} 題`), h('td', {}, m.concept_name)))))),
    h('p', { class: 'muted' }, `概念與題目清單 v${s.concept_list_version}`),
    h('h2', {}, '作答狀態'),
    h('div', { class: 'grid4' },
      h('div', { class: 'stat' }, h('span', { class: 'muted' }, '同意研究學生'), h('b', {}, `${s.consented_n} 人`)),
      h('div', { class: 'stat' }, h('span', { class: 'muted' }, '已完成小考'), h('b', {}, `${s.completed_n} 人`)),
      h('div', { class: 'stat' }, h('span', { class: 'muted' }, '未完成'), h('b', {}, `${s.not_completed_n} 人`)),
      h('div', { class: 'stat' }, h('span', { class: 'muted' }, '未作答欄位'), h('b', {}, `${s.missing_cells} 格`))),
    h('div', { class: 'notice' }, h('b', {}, '提醒：'), '此頁不顯示個人名單、代號、答對率或各題結果。確認後，本輪小考資料將鎖定，並開放教師洞察卡。'),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: teacherUI.map_ok, onchange: () => { teacherUI.map_ok = !teacherUI.map_ok; render(); } }), '我確認題目與概念對應正確'),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: teacherUI.closed_ok, onchange: () => { teacherUI.closed_ok = !teacherUI.closed_ok; render(); } }), '我確認本輪小考已截止'),
    h('div', { class: 'row between' }, h('span', { class: 'muted' }, can ? '可以確認' : '兩項都勾選後才能確認'),
      h('button', { class: 'btn primary', disabled: !can, onclick: () => run(async () => {
        const r = await api('POST', '/api/teacher/quiz_confirm', { map_ok: true, closed_ok: true }, TEACHER);
        toast(r.cards ? `已鎖定小考資料，產生 ${r.cards} 張洞察卡` : '已鎖定小考資料；本輪沒有概念達到出卡門檻');
        render();
      }) }, '確認並查看洞察卡')));
}

async function cardsView() {
  const { cards, analyzable_rule } = await api('GET', '/api/teacher/cards', null, TEACHER);
  if (!cards.length) {
    return [h('section', { class: 'card' }, h('h2', {}, '本輪沒有概念達到出卡門檻'), h('p', {}, `出卡門檻：${analyzable_rule}。沒有出卡也是一種結果，會一併記錄在研究資料中。`))];
  }
  return cards.map((c, i) => insightCard(c, i, cards.length));
}

function insightCard(c, i, total) {
  let btn;
  const d = c.display;
  const st = teacherUI.resp[c.card_id] || (teacherUI.resp[c.card_id] = { pick: null, note: '' });
  const body = [
    h('div', { class: 'row between muted' }, h('span', {}, `本輪洞察卡 ${i + 1} / ${total}`), h('span', {}, '請先完成事前預判與小考，再看卡片')),
    h('div', { class: 'row' }, h('span', { class: 'muted' }, '單元'), h('b', {}, c.unit_name)),
    h('div', {}, h('span', { class: 'tag' }, '候選共同學習困難')),
    h('h1', {}, d.concept_name),
    h('div', { class: 'row between' }, h('h2', {}, '訊號'), h('span', { class: 'muted' }, `分母：${d.denominator_text}（${d.denominator_note}）`)),
    d.signals.map((s) => h('div', { class: 'sig' },
      h('span', {}, s.label),
      h('span', { class: 'bar' }, s.masked ? null : h('span', { style: `width:${s.pct}%` })),
      h('span', { class: 'n', style: s.masked ? 'color:var(--quiet)' : 'font-weight:700' }, s.text))),
    h('p', { class: 'muted' }, '四種訊號分開計算。同一位學生可能同時出現多種訊號，人數不能相加。'),
    h('h2', {}, '常見困惑'),
    c.confusion_summary ? h('div', { class: 'quote' }, c.confusion_summary) : h('div', { class: 'quote muted' }, '提問少於 5 位學生，不產生常見困惑摘要。'),
    h('h2', {}, 'AI 輔助建議', h('span', { style: 'font-weight:400;color:var(--quiet)' }, '｜請依課程情況判斷是否採用')),
    h('p', {}, c.ai_suggestion),
    h('p', { class: 'muted' }, '假資料版未串外部 AI，這段由研究者預先撰寫。'),
  ];
  if (c.response) {
    body.push(h('div', { class: 'card soft' },
      h('div', { class: 'row between' }, h('b', {}, `教師回應：${c.response.label}`), h('span', { class: 'muted' }, `送出時間 ${c.response.submitted_at}｜已鎖定`)),
      h('p', {}, c.response.note),
      c.action_notes.map((n) => h('p', { class: 'muted' }, `補記（${n.server_at}）：${n.note}`)),
      noteBox(c)));
  } else {
    const can = st.pick && st.note.trim();
    body.push(h('div', { class: 'card soft' },
      h('fieldset', { style: 'border:0;margin:0;padding:0;display:flex;flex-direction:column;gap:10px' },
        h('legend', { style: 'font-weight:700;margin-bottom:8px' }, '教師回應'),
        h('div', { class: 'grid4' }, RESPONSES.map(([k, label]) => h('label', { class: 'choice' + (st.pick === k ? ' on' : '') },
          h('input', { type: 'radio', name: 'resp-' + c.card_id, checked: st.pick === k, onchange: () => { st.pick = k; render(); } }), label)))),
      h('label', { for: 'note-' + c.card_id }, h('b', {}, '處理方式或判斷原因')),
      h('span', { class: 'muted' }, '請簡述您採取的教學做法，或未採納此卡的原因。'),
      h('textarea', { id: 'note-' + c.card_id, rows: 3, placeholder: st.pick ? RESPONSE_EXAMPLE[st.pick] : '先選上面的回應',
        oninput: (e) => { st.note = e.target.value; btn.disabled = !(st.pick && st.note.trim()); } }, st.note),
      h('div', { class: 'row between' }, h('span', { class: 'muted' }, '回應和原因都填了才能送出；送出後不能修改。'), btn = h('button', { class: 'btn primary', disabled: !can, onclick: () => run(async () => {
        await api('POST', `/api/teacher/cards/${c.card_id}/response`, { response: st.pick, note: st.note }, TEACHER);
        toast('已送出回應');
        render();
      }) }, '送出回應'))));
  }
  return h('section', { class: 'card' }, body);
}

function noteBox(c) {
  const ta = h('textarea', { rows: 2, placeholder: '送出後才發生的實際做法，可在這裡補記（不改原本的回應）', 'aria-label': '補記' });
  return h('div', { class: 'row' }, ta, h('button', { class: 'btn', onclick: () => run(async () => {
    await api('POST', `/api/teacher/cards/${c.card_id}/note`, { note: ta.value }, TEACHER);
    render();
  }) }, '補記'));
}

// ── 研究者 ───────────────────────────────────────────
async function renderResearch() {
  setWho('研究者');
  const d = await api('GET', '/api/research/cards', null, RESEARCHER);
  const download = () => run(async () => {
    const res = await fetch('/api/research/export.csv', { headers: { 'X-Demo-Account': RESEARCHER } });
    if (!res.ok) throw new Error((await res.json()).error);
    const url = URL.createObjectURL(await res.blob());
    const a = h('a', { href: url, download: 'learntrace_rq1_export.csv' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  show(
    h('a', { href: '#/' }, '← 回首頁'),
    h('section', { class: 'card' },
      h('h1', {}, '研究者檢視：洞察卡追溯'),
      h('p', { class: 'muted' }, '這一頁顯示精確人數與來源事件，教師端看不到。只用研究代號。'),
      h('div', { class: 'row' }, h('span', {}, `階段：${d.stage}`), h('span', {}, `本輪可分析學生 ${d.analyzable_n} / 同意研究 ${d.consented_n}`),
        h('span', {}, d.threshold ? `出卡門檻：至少 ${d.threshold} 人` : '尚無可分析學生')),
      h('div', { class: 'scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '概念'), Object.values(SIGNAL_LABELS).map((l) => h('th', {}, l)), h('th', {}, '任一訊號'))),
        h('tbody', {}, d.concepts.map((c) => h('tr', {}, h('td', {}, c.name), Object.keys(SIGNAL_LABELS).map((s) => h('td', {}, c.counts[s])), h('td', {}, c.union)))))),
      h('button', { class: 'btn', style: 'align-self:flex-start', disabled: d.analyzable_n === 0, onclick: download }, '下載 RQ1 匯出（CSV）')),
    d.traces.length ? d.traces.map(traceView) : h('section', { class: 'card' }, h('p', {}, '目前沒有洞察卡可追溯。')));
}

function traceView(t) {
  return h('section', { class: 'card' },
    h('div', { class: 'row between' }, h('h2', {}, `追溯 ${t.card_id}`),
      h('span', { class: t.all_match ? 'ok' : 'bad' }, t.all_match ? '卡片數字與事件紀錄一致' : '不一致，請檢查')),
    h('p', { class: 'muted' }, `卡片產生於 ${t.generated_at}；可分析學生 卡片 ${t.analyzable_n}／重算 ${t.analyzable_recomputed}`),
    t.signals.map((s) => h('details', {},
      h('summary', {}, `${s.label}：卡片 ${s.card_count}，事件重算 ${s.recomputed} ${s.match ? '✓' : '✗'}`),
      h('div', { class: 'scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '研究代號'), h('th', {}, 'log_id'), h('th', {}, '事件'), h('th', {}, '伺服器時間'), h('th', {}, '內容'))),
        h('tbody', {}, s.evidence.map((e) => h('tr', {}, h('td', { class: 'mono' }, e.research_code), h('td', { class: 'mono' }, e.log_id),
          h('td', {}, e.event_type), h('td', { class: 'mono' }, e.server_at), h('td', { class: 'mono' }, JSON.stringify(e.payload))))))))));
}

render();
