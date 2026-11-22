/* Sai2-Timelapse🔔 · 前端逻辑
 *
 * 和后端的约定：
 *   - 启动时 bootstrap() 拿一份完整快照（文案 / 设置 / 状态 / 日志）
 *   - 之后每 150ms poll(sinceLogId) 拉增量：状态 + 新日志
 *   - 改任何设置都整体推 set_settings({key: value})，后端只落变化的项
 *
 * 为什么轮询而不是后端推：WebView 的 GUI 线程不归我们管，
 * 跨线程 evaluate_js 在不同后端行为不一致；轮询 150ms 的代价可以忽略。
 */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview?.api;

/* 出错了得留个痕迹。窗口里没有控制台，探针也读不到 console，
 * 就把最后一条错误挂到 window.__err 上，探针用 evaluate_js 取回来。 */
window.addEventListener('error', (e) => {
  window.__err = `${e.message} @${(e.filename || '').split('/').pop()}:${e.lineno}`;
});
window.addEventListener('unhandledrejection', (e) => {
  window.__err = 'rejection: ' + (e.reason?.message || e.reason);
});

let S = {};            // strings
let settings = {};     // 当前设置
let status = {};       // 当前状态
let lastLogId = 0;
let settingsRev = 0;
let canvasRev = -1;
let previewData = null;
let previewRev = -1;
let booted = false;
// 上一次看到的导出结果路径。用它判断「是不是换了一支新成片」，
// 决定要不要重新显示「归档」按钮。
let lastExported = '';
// 导出前拦截阈值：少于这么多帧，成片不足 1 秒（按 30fps 算）——
// 播放器一闪而过，用户会以为导出坏了。低于它就先弹窗说清楚。
const MIN_EXPORT_FRAMES = 30;

// Presentation-only strings; business messages still come from the backend.
const UX = {
  zh: { failed: '操作未完成，请重试或查看日志。', copied: '日志已复制', cleared: '日志已清空',
        saved: '设置已更新', size: '自定义像素上限',
        ready: '请先选择保存位置和画布',
        fb_copied: 'QQ 群号已复制，去 QQ 搜索加群',
        on: '开', off: '关',
        noCanvasFound: '（没读到画布 —— 日志里有原因）',
        noSaiRunning: '（没有运行中的 SAI）',
        fewTitle: '帧数很少',
        fewBody: '只有 {n} 帧，成片大约 {s} 秒。缩时录制只在画面变化时记一帧，没画的时间不会被记进去。确定要导出吗？',
        fewOk: '仍然导出',
        archived: '已归档 {} 帧，下次导出从新的一段开始',
        archive_empty: '没有可归档的帧',
        segments: '素材分 {} 段存在磁盘上，导出时会按顺序合成一支完整视频' },
  en: { failed: 'Could not complete the action. Retry or check the log.', copied: 'Log copied',
        cleared: 'Log cleared', saved: 'Settings updated', size: 'Custom pixel limit',
        ready: 'Choose a save location and canvas first',
        fb_copied: 'QQ group number copied — search it in QQ to join',
        on: 'On', off: 'Off',
        noCanvasFound: '(no canvas found — see the log)',
        noSaiRunning: '(SAI is not running)',
        fewTitle: 'Very few frames',
        fewBody: 'Only {n} frames — about {s} s of video. Timelapse records one frame each time the canvas changes, so time spent not drawing is not counted. Export anyway?',
        fewOk: 'Export anyway',
        archived: 'Archived {} frames — the next export starts a new clip',
        archive_empty: 'Nothing to archive',
        segments: '{} segments on disk — export joins them into one video' },
  ja: { failed: '操作に失敗しました。再試行するかログを確認してください。', copied: 'ログをコピーしました',
        cleared: 'ログを消去しました', saved: '設定を更新しました', size: 'カスタムピクセル上限',
        ready: '保存先とキャンバスを先に選択してください',
        fb_copied: 'QQ グループ番号をコピーしました。QQ で検索して参加してください',
        on: 'オン', off: 'オフ',
        noCanvasFound: '（キャンバスを読み取れません — ログを確認）',
        noSaiRunning: '（SAI が起動していません）',
        fewTitle: 'フレーム数が少なめです',
        fewBody: 'わずか {n} フレーム、再生時間は約 {s} 秒です。タイムラプスはキャンバスが変化した時だけ 1 フレーム記録するため、描いていない時間は含まれません。書き出しますか？',
        fewOk: '書き出す',
        archived: '{} フレームをアーカイブしました。次回の書き出しは新しいクリップになります',
        archive_empty: 'アーカイブするフレームがありません',
        segments: '素材は {} 個に分割されています。書き出し時に 1 本の動画に結合されます' },
};
// 反馈群号。改号要同时改 index.html 里那个 <b>。
const FEEDBACK_QQ = '942992768';
const ux = (key) => (UX[settings.lang === 'en' ? 'en' : settings.lang === 'ja' ? 'ja' : 'zh'])[key];
let noticeTimer;
function notify(text, kind = 'info') {
  const el = $('uiNotice');
  clearTimeout(noticeTimer);
  el.dataset.kind = kind;
  setText(el, text);
  el.hidden = false;
  noticeTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 6500 : 3200);
}
// 悬浮条开关。老配置文件里没有 floatbar 这个键，缺省按「开」处理 ——
// 否则升级上来的用户会以为自己功能丢了。
const floatOn = () => settings.floatbar !== false;
function renderFloatBtn() {
  const el = $('floatState');
  if (!el) return;
  el.textContent = floatOn() ? ux('on') : ux('off');
  $('floatBtn').classList.toggle('on', floatOn());
}

const busyButtons = new Set();
async function runAction(id, action, success) {
  if (busyButtons.has(id)) return;
  const btn = $(id);
  busyButtons.add(id);
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  try {
    const result = await action();
    if (success) notify(ux(success));
    return result;
  } catch (e) {
    window.__lastActionError = String(e);
    notify(ux('failed'), 'error');
  } finally {
    busyButtons.delete(id);
    btn.removeAttribute('aria-busy');
    btn.disabled = false;
    renderButtons();
  }
}

const clamp = (v, lo, hi, dflt) => {
  const n = parseFloat(v);
  if (!isFinite(n)) return dflt;
  return Math.min(Math.max(n, lo), hi);
};

const mmss = (sec) => {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
};

/* 文件大小。用 1024 进制（和资源管理器一致），别用 1000 —— 用户会拿它
 * 跟文件夹里看到的大小对照，对不上就是"你这个数不准"。 */
const fmtBytes = (bytes) => {
  const n = Number(bytes) || 0;
  if (n >= 1024 * 1024 * 1024) return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
};

/* 主题名和语言名是专有名词，不翻译（和 Tk 版一致）。
 * 键名沿用旧配置 sai-dark / sai-light，只是标签改叫黑 / 白。 */
const THEMES = [['sai-dark', '黑'], ['sai-light', '白']];
const LANGS = [['zh_CN', '简体中文'], ['en', 'English'], ['ja', '日本語']];

/* 录制分辨率的快捷档位：[像素数, 标签]。空标签 = 「原始尺寸」（16384 是上限，
 * 等于不缩）。这张表要放在模块级：下拉的 option value 存的就是**像素数**，
 * change 事件里得靠它反查，早先它是 renderSettings 里的局部变量，结果那边
 * 只能拿到 option 的索引 —— 选「4K」存进去的是 3，后端再夹到 100，
 * 录出来只有 100×100，界面上却只多出一行「3 px」，完全看不出问题。 */
const SIZE_PRESETS = [[1280, '720p'], [1920, '1080p'], [2560, '1440p'],
                      [3840, '4K'], [16384, '']];
const ORIGINAL_SIZE = 16384;

/* ------------------------------------------------------------------ 文案 */
function applyStrings() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.dataset.i18n;
    if (!S[key]) return;
    // 只替换纯文本节点，别把 <svg>/<span> 之类的子元素冲掉
    if (el.children.length === 0) el.textContent = S[key];
    else {
      const span = el.querySelector('span:not([class])') || el.querySelector('span');
      if (span) span.textContent = S[key];
    }
  });
  document.querySelectorAll('[data-tip]').forEach((el) => {
    const tip = S[el.dataset.tip];
    if (tip) el.title = tip;
  });
  document.documentElement.lang = settings.lang === 'en' ? 'en' : settings.lang === 'ja' ? 'ja' : 'zh-CN';
  document.querySelectorAll('.row').forEach((row, i) => {
    const label = row.querySelector('label');
    const fields = row.querySelectorAll('input, select');
    if (!label) return;
    label.id = label.id || `fieldLabel${i}`;
    fields.forEach((field) => field.setAttribute('aria-labelledby', label.id));
    if (fields[0]) label.htmlFor = fields[0].id;
    if (label.title && fields[0]) fields[0].title = label.title;
  });
  $('sizeValue').removeAttribute('aria-labelledby');
  $('sizeValue').setAttribute('aria-label', ux('size'));
  $('logBody').setAttribute('aria-label', S.status || 'Log');
  $('previewBox').setAttribute('aria-label', S.canvas_preview || 'Canvas preview');
  $('expProgress').setAttribute('aria-label', S.export || 'Export');
  // 预览框的文案不挂 data-i18n：它里面可能有 <img>，applyStrings 会把整块
  // textContent 换掉，换语言时图就没了（而 renderPreview 见 rev 没变不会补回来）。
  if (!previewData) $('previewBox').textContent = S.no_preview || '';
  // 动态按钮文字
  renderButtons();
  renderFloatBtn();
  fitLabelColumn();
}

/* 标签列宽按当前语言里最长的那条算 —— 中/英/日三种语言长度差很多，
 * 写死一个 px 不是截断就是浪费。
 *
 * 不能直接量元素自己的 scrollWidth：不在当前页签里的 .page 是 display: none，
 * 隐藏子树里的 scrollWidth 恒为 0，量出来的「最长标签」只覆盖了可见的那一页 ——
 * 导出页的「开头先显示末帧预览」就因此被省略号截掉了。
 * 改成拿一个脱离布局的探针，用同样的字号量文字宽度，与元素是否可见无关。 */
const LABEL_MAX_W = 150;    /* 列宽上限。日语最长的标签要 216px，真按它排整列太浪费 */

function fitLabelColumn() {
  let probe = document.getElementById('labelProbe');
  if (!probe) {
    probe = document.createElement('span');
    probe.id = 'labelProbe';
    document.body.appendChild(probe);
  }
  const items = [];
  let widest = 0;
  document.querySelectorAll('.row > label').forEach((el) => {
    const text = el.textContent;
    if (!text) { el.classList.remove('wrap'); return; }
    probe.textContent = text;
    const w = probe.getBoundingClientRect().width;
    items.push([el, w]);
    if (w > widest) widest = w;
  });
  if (!widest) return;
  const col = Math.min(Math.ceil(widest) + 2, LABEL_MAX_W);
  document.documentElement.style.setProperty('--label-w', `${col}px`);
  // 列宽封顶后还放不下的，改成两行显示 —— 宁可行高一点，也别用省略号把字吞掉
  items.forEach(([el, w]) => el.classList.toggle('wrap', w > col));
}

/* ------------------------------------------------------------------ 设置渲染 */
function fillSelect(el, values, selectedIndex) {
  el.innerHTML = '';
  values.forEach((text, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = text;
    el.appendChild(opt);
  });
  el.value = String(selectedIndex ?? 0);
}

/* 让分辨率下拉的选中项跟像素数对齐。不是预设档位（用户手填的数）就临时补一项 ——
 * 否则下拉会停在旧档位上，和旁边数字框显示的完全对不上。补过的自定义项下次同步
 * 时先清掉，避免来回填几个数就攒出一串「xxx px」。 */
function syncSizePreset(px) {
  const sel = $('sizePreset');
  if (!sel) return;
  [...sel.options].forEach((o) => {
    if (!SIZE_PRESETS.some(([p]) => p === Number(o.value))) o.remove();
  });
  if (!SIZE_PRESETS.some(([p]) => p === px)) {
    const opt = document.createElement('option');
    opt.value = String(px);
    opt.textContent = `${px} px`;
    sel.appendChild(opt);
  }
  sel.value = String(px);
}

function renderSettings(force = false) {
  const set = (id, value) => {
    const el = $(id);
    if (!el) return;
    // 用户正在这个框里打字时别覆盖他的内容
    if (!force && document.activeElement === el) return;
    if (el.type === 'checkbox') el.checked = !!value;
    else el.value = value ?? '';
  };

  set('framesFile', settings.frames_file);
  set('autoSplit', settings.auto_split_sai);
  set('appendLast', settings.append_last_session);
  set('sizeValue', settings.image_size_limit);
  set('recContainer', settings.recording_custom_container);
  set('recCodec', settings.recording_custom_codec);
  set('exportFps', settings.export_fps);
  set('exportLimit', settings.export_time_limit);
  set('previewFirst', settings.export_preview_last_frame);
  set('previewDuration', settings.export_preview_duration);
  set('useRecType', settings.export_user_recording);
  set('expContainer', settings.export_custom_container);
  set('expCodec', settings.export_custom_codec);

  $('framesReadonly').textContent = settings.frames_file || '';
  $('framesReadonly').classList.toggle('empty', !settings.frames_file);

  fillSelect($('recType'), window.__videoTypes || [], settings.recording_type);
  fillSelect($('expType'), window.__videoTypes || [], settings.export_type);
  fillSelect($('versionSel'), window.__saiVersions || [], settings.sai_version_override);

  // 主题 / 语言下拉：选项是固定表，选中值来自设置
  const fillNamed = (el, pairs, value) => {
    el.innerHTML = '';
    pairs.forEach(([key, label]) => {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = label;
      el.appendChild(opt);
    });
    el.value = value;
  };
  fillNamed($('themeSel'), THEMES, settings.theme || 'sai-dark');
  fillNamed($('langSel'), LANGS, settings.lang || 'zh_CN');

  /* 分辨率下拉：option 的 value 直接放**像素数**，不走 fillSelect 的索引方案。
   * 索引方案在 change 里太容易拿错 —— 这里以前就是拿索引当像素数用，
   * 选「720p」得到 0（被 if (px > 0) 吞掉，点了没反应），选「4K」得到 3。 */
  const presetText = (px, label) => label ? `${label} (${px} px)` : S.original_size;
  const sizeSel = $('sizePreset');
  sizeSel.innerHTML = '';
  SIZE_PRESETS.forEach(([px, label]) => {
    const opt = document.createElement('option');
    opt.value = String(px);
    opt.textContent = presetText(px, label);
    sizeSel.appendChild(opt);
  });
  syncSizePreset(Number(settings.image_size_limit) || 1920);

  // 自定义容器/编码器只在选了「自定义」时露出来
  const custom = (type) => String(type) === '2';
  $('recContainerRow').hidden = !custom(settings.recording_type);
  $('recCodecRow').hidden = !custom(settings.recording_type);
  const useRec = !!settings.export_user_recording;
  $('expTypeRow').hidden = useRec;
  $('expContainerRow').hidden = useRec || !custom(settings.export_type);
  $('expCodecRow').hidden = useRec || !custom(settings.export_type);

  applyTheme(settings.theme);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'sai-light' ? 'sai-light' : 'sai-dark';
}

/* ------------------------------------------------------------------ 状态渲染 */
function renderButtons() {
  const mode = status.op?.mode || null;
  const paused = !!status.op?.paused;
  const recBtn = $('recBtn');
  const expBtn = $('expBtn');
  const pauseBtn = $('pauseBtn');

  if (mode === 'record') {
    recBtn.textContent = S.stop_recording || '';
    recBtn.className = 'btn-danger';
    recBtn.disabled = false;
    expBtn.disabled = true;
    // 暂停按钮只在录制中出现，不录制时整颗藏掉 —— 常驻一个灰按钮会让人
    // 以为「现在能暂停」，其实是不可用的状态。
    pauseBtn.hidden = false;
    pauseBtn.textContent = paused ? (S.resume || '') : (S.pause || '');
    pauseBtn.classList.toggle('paused', paused);
  } else {
    recBtn.textContent = S.start_recording || '';
    recBtn.className = 'btn-primary';
    recBtn.disabled = mode === 'export';
    pauseBtn.hidden = true;
    pauseBtn.classList.remove('paused');
  }

  if (mode === 'export') {
    expBtn.textContent = S.stop_exporting || '';
    expBtn.className = 'btn-danger';
    expBtn.disabled = false;
  } else {
    expBtn.textContent = S.export || '';
    expBtn.className = 'btn-primary';
    expBtn.disabled = mode === 'record';
  }
  // 没选保存位置时，主按钮点下去是弹引导框而不是开始录制 —— 先在这里说清楚，
  // 别让用户以为按钮坏了。有位置之后立刻摘掉，不留多余的 tooltip。
  const tip = settings.frames_file ? '' : ux('ready');
  [recBtn, expBtn].forEach((btn) => {
    if (tip) btn.title = tip;
    else btn.removeAttribute('title');
  });
  for (const id of busyButtons) $(id).disabled = true;
  pauseBtn.setAttribute('aria-pressed', String(paused));
}

/* 按顺序填 {} 占位符。文案表里的多占位符模板（如「{} 帧 · {}」）用它，
 * 免得每处各写一遍 replace 链。 */
function fmt(template, ...args) {
  let i = 0;
  return String(template == null ? '' : template).replace(/\{\}/g, () => {
    const v = args[i++];
    return v == null ? '' : v;
  });
}

/* ---------------------------------------------------------------- 确认框 */
let confirmOnOk = null;
let confirmKind = '';
const modalReturnFocus = new Map();
function showModal(id, focusId) {
  modalReturnFocus.set(id, document.activeElement);
  $(id).hidden = false;
  document.querySelectorAll('.appbar, .tabs, .content, .footer').forEach((el) => { el.inert = true; });
  $(focusId).focus();
}
function hideModal(id) {
  $(id).hidden = true;
  const another = document.querySelector('.modal-mask:not([hidden])');
  document.querySelectorAll('.appbar, .tabs, .content, .footer').forEach((el) => { el.inert = !!another; });
  const previous = modalReturnFocus.get(id);
  if (!another && previous?.isConnected) previous.focus();
  modalReturnFocus.delete(id);
}

/* 不用原生 confirm()：它阻塞 JS 线程，弹出期间轮询停摆、录制时长冻在那一刻；
 * 而且白底系统框压在深色界面上，跟整个设计不是一回事。 */
function ask(title, body, okText, onOk, kind = 'stop') {
  $('confirmTitle').textContent = title;
  $('confirmBody').textContent = body;
  $('confirmOk').textContent = okText;
  // 只有「停止录制」这种会丢掉正在录的东西的操作才配红按钮。
  // 「帧数少但仍要导出」是用户自己的判断，染成警示色等于替他做决定。
  $('confirmOk').className = kind === 'stop' ? 'btn-danger' : 'btn';
  confirmOnOk = onOk;
  confirmKind = kind;
  showModal('confirmMask', 'confirmCancel');
}

function closeConfirm() {
  if ($('confirmMask').hidden) return;
  hideModal('confirmMask');
  confirmOnOk = null;
  confirmKind = '';
}

/* 首次启动的保存位置引导。
 *
 * 这个软件要发布给别人用，而「帧目录」这种东西对没接触过的人是零概念 ——
 * 留一个空框让他自己猜，很可能录完发现东西不知道去哪了，或者更糟：直接
 * 录到程序目录里。所以第一次打开就拦下来，必须选一个位置才能用。 */
let defaultSaveDir = '';

function openSetup() {
  $('setupPath').textContent = defaultSaveDir || '';
  if ($('setupMask').hidden) showModal('setupMask', 'setupUse');
}

function closeSetup() {
  hideModal('setupMask');
}

function applySaveDir(path) {
  if (!path) return;          // 用户在系统对话框里点了取消：引导框留着
  settings.frames_file = path;
  renderSettings(true);
  closeSetup();
}

function askStopRecording() {
  const op = status.op || {};
  ask(
    S.confirm_stop_title || '',
    fmt(S.confirm_stop_body, op.frames || 0, mmss(op.elapsed || 0)),
    S.stop_recording || '',
    // 用 stop_operation 而不是 toggle_record：从弹框到点确认之间，录制可能
    // 已经被热键停掉了，那时 toggle 会反过来**开始**一段新录制。
    () => runAction('recBtn', () => api().stop_operation()),
  );
}

/* textContent 赋值即使字符串一模一样，也会把原来的文本节点删掉重建 —— 而
 * renderStatus 是每 150ms 跑一次的，这么做等于每 150ms 让一次布局失效。
 * 拖动窗口时这些失效会挤进同一帧里。值没变就别碰。 */
function setText(el, text) {
  const value = text == null ? '' : text;
  if (el && el.textContent !== value) el.textContent = value;
}

function renderStatus(st) {
  if (!st) return;
  status = st;

  // 顶栏录制胶囊
  const pill = $('recPill');
  const recording = st.op?.mode === 'record';
  const paused = !!st.op?.paused;
  pill.classList.toggle('on', recording);
  pill.classList.toggle('paused', recording && paused);
  if (recording) {
    const label = paused
      ? (S.paused || S.recording || '')
      : (S.recording || 'Recording');
    setText($('recPillText'),
      `${label} ${st.op.frames} · ${mmss(st.op.elapsed)}`);
  }

  renderButtons();

  // 确认框开着时录制被别处停掉了（热键 / 画布被关）→ 自动收起。
  // 否则用户对着「结束录制？」点下去，其实是在对一个已经结束的会话发呆。
  if (!$('confirmMask').hidden && confirmKind === 'stop' && !recording) closeConfirm();

  // SAI 版本行
  const verEl = $('saiVersion');
  if (st.sai.state === 'running') {
    setText(verEl, st.sai.version + (st.sai.override ? ' ' + (S.override || '') : ''));
    verEl.classList.remove('empty');
  } else {
    setText(verEl, S.sai_not_running || '');
    verEl.classList.add('empty');
  }

  // 画布下拉：只在列表变化时重建，否则用户展开下拉会被打断
  if (st.sai.rev !== canvasRev) {
    canvasRev = st.sai.rev;
    const sel = $('canvasSel');
    const previous = settings.sai_canvas;
    sel.innerHTML = '';
    st.sai.canvases.forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    if (st.sai.canvases.length === 0) {
      // 空下拉框看起来像「下拉坏了」。给一条不可选的占位说明，
      // 用户才知道是「没读到画布」而不是「这里本来没东西」。
      const opt = document.createElement('option');
      opt.value = '';
      opt.disabled = true;
      opt.textContent = st.sai.state === 'running' ? ux('noCanvasFound') : ux('noSaiRunning');
      sel.appendChild(opt);
    }
    if (st.sai.canvases.includes(previous)) sel.value = previous;
  }

  // 导出页的「本次录制」
  const summary = st.export?.summary || '';
  setText($('summary'), summary);
  $('summary').classList.toggle('empty', !summary);

  // 素材分了多段就说一句。不是提示「有问题」，是防止用户去帧目录里翻，
  // 把那堆十几秒的原始分段当成导出结果。
  const segments = st.export?.segments || 0;
  const segEl = $('segmentsHint');
  if (segEl) {
    if (segments > 1) {
      setText(segEl, fmt(ux('segments'), segments));
      segEl.classList.remove('off');
    } else {
      segEl.classList.add('off');
    }
  }

  // 导出结果
  const saved = st.export?.result || '';
  setText($('savedTo'), saved);
  $('savedTo').classList.toggle('empty', !saved);
  $('openExport').disabled = !saved || busyButtons.has('openExport');
  $('savedTo').title = saved;
  $('framesReadonly').title = settings.frames_file || '';
  // 花标只在"本次导出成功"时出现，下次导出前不累积。
  // 注意用 class 而不是 .hidden：<svg> 没有 HTMLElement.hidden。
  $('flowerOk').classList.toggle('on', !!saved);

  // 成片卡片。后端读不到文件大小 / 回读帧数时 info 会是空字典，
  // 那时整张卡片不出现，退回上面那一行路径 —— 不要半残的卡片。
  const info = st.export?.info || {};
  const showCard = !!saved;
  $('resultCard').hidden = !showCard;
  if (saved !== lastExported) {
    lastExported = saved;
    // 换了一支新成片 -> 重新给一次归档的机会；归档过的那支不再重复弹出
    $('archiveFrames').hidden = !saved;
  }
  if (showCard) {
    const thumb = $('resultThumb');
    const src = info.thumb || '';
    // 缩略图是 data URL，每 150ms 重新赋一次 innerHTML 等于每 150ms 重建一张图
    if (thumb.dataset.src !== src) {
      thumb.dataset.src = src;
      thumb.innerHTML = '';
      if (src) {
        const im = new Image();
        im.src = src;
        thumb.appendChild(im);
      }
    }
    setText($('resultName'), info.name || saved.split(/[\\/]/).pop());
    $('resultName').title = saved;
    thumb.hidden = !src;
    const bits = [];
    if (info.size) bits.push(fmtBytes(info.size));
    if (info.duration) bits.push(mmss(info.duration));
    if (info.frames) bits.push(`${info.frames} ${S.frames_unit || ''}`.trim());
    setText($('resultMeta'), bits.join(' · '));
  }

  // 导出进度条。以前是把它并进按钮文字（「停止导出 · 44 / 105」），但按钮宽度
  // 会跟着数字变，每帧都在挪；而且一眼看不出还剩多少。拆出来单独画。
  const exporting = st.op?.mode === 'export';
  $('expProgress').hidden = !exporting;
  if (exporting) {
    const frac = Math.max(0, Math.min(1, Number(st.op.progress) || 0));
    $('expFill').style.width = `${(frac * 100).toFixed(2)}%`;
    const pct = Math.round(frac * 100);
    $('expProgress').setAttribute('aria-valuenow', String(pct));
    // 帧数比百分比准：百分比四舍五入后会有"卡在 99%"的错觉，帧数是实数。
    setText($('expPct'), st.op.label ? `${pct}% · ${st.op.label}` : `${pct}%`);
  }

  // 热键提示：注册失败就整行撤掉
  const hint = $('hotkeyHint');
  setText(hint, st.hotkey?.text || '');
  hint.classList.toggle('off', !st.hotkey?.ok);
}

/* 预览图。rev 是后端的「换图次数」，比 data URL 更可靠：
 * 同一张画布重抓可能得到一模一样的字符串，光比 URL 会把「真的换了图」
 * 误判成「没变化」，切画布时就卡在旧图上不动了。 */
function renderPreview(dataUrl, rev, size) {
  const nextRev = Number.isFinite(rev) ? rev : null;
  if (nextRev !== null) {
    if (nextRev === previewRev) return;
    previewRev = nextRev;
  } else if (dataUrl === previewData) {
    return;
  }
  previewData = dataUrl;
  const box = $('previewBox');
  if (!dataUrl) {
    box.textContent = S.no_preview || '';
    setText($('previewSize'), '');
    return;
  }
  box.innerHTML = '';
  const img = new Image();
  img.src = dataUrl;
  // 尺寸用后端给的**真实画布像素**，不是这张图的 naturalWidth ——
  // 预览图已经被缩到 400 以内，拿它量出来的只是缩略图大小。
  const w = size && size[0];
  const h = size && size[1];
  if (w && h) setText($('previewSize'), `${w} × ${h}`);
  else setText($('previewSize'), '');
  box.appendChild(img);
}

/* ------------------------------------------------------------------ 日志 */
const LOG_MAX = 1500;

function appendLog(entries) {
  const body = $('logBody');
  if (!entries || !entries.length) return;

  // 贴底时才自动滚动：用户在往上翻旧日志时不该被拽回来
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;

  for (const entry of entries) {
    if (entry.id > lastLogId) lastLogId = entry.id;
    if (!entry.text) continue;
    const line = document.createElement('div');
    line.className = 'line ' + (entry.level || 'info');
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = entry.t;
    line.appendChild(ts);
    line.appendChild(document.createTextNode(entry.text));
    body.appendChild(line);
  }

  while (body.childElementCount > LOG_MAX) body.removeChild(body.firstElementChild);
  if (atBottom) body.scrollTop = body.scrollHeight;
  syncLogEmpty();
}

/* 空态提示跟着日志行数走。日志被清空（按钮 / 后端 clear）之后也要补回来，
 * 所以除了 appendLog，清空的地方也得调一次。 */
function syncLogEmpty() {
  $('logEmpty').hidden = $('logBody').childElementCount > 0;
}

/* ------------------------------------------------------------------ 通信 */
async function bootstrap() {
  const data = await api().bootstrap();
  S = data.strings || {};
  settings = data.settings || {};
  window.__videoTypes = data.video_types || [];
  window.__saiVersions = data.sai_versions || [];
  defaultSaveDir = data.default_save_dir || '';
  settingsRev = data.status?.settings_rev ?? 0;

  $('ver').textContent = data.version ? `v${data.version}` : '';
  applyStrings();
  renderSettings(true);
  $('logBody').innerHTML = '';
  lastLogId = 0;
  appendLog(data.log || []);
  syncLogEmpty();
  renderStatus(data.status);
  renderPreview(data.preview, data.status?.preview_rev, data.status?.preview_size);
  booted = true;
  exposeTestApi();
  // 首次启动：没有保存位置就先把引导框拉出来，别让用户对着空框录
  if (!settings.frames_file) openSetup();
}

let polling = false;
async function poll() {
  if (!booted || polling) return;
  polling = true;
  try {
    // 带上手里的 rev：后端见它没变就不重复回传那张几万字符的图
    const data = await api().poll(lastLogId, previewRev);
    renderStatus(data.status);
    appendLog(data.log);
    renderPreview(data.preview, data.status?.preview_rev, data.status?.preview_size);

    // 后端改过设置（恢复默认 / 切语言 / 换画布）→ 重新拉一份全量
    const rev = data.status?.settings_rev ?? 0;
    if (rev !== settingsRev) {
      settingsRev = rev;
      await bootstrap();
    }
  } catch (e) {
    // 窗口正在关闭时请求会失败，忽略即可
  } finally {
    polling = false;
  }
}

/* 设置变更：整体推过去，后端只落变化的项 */
let pushTimer = null;
const pending = {};
let settingWrite = Promise.resolve();
function flushSettings() {
  clearTimeout(pushTimer);
  const write = async () => {
    const patch = { ...pending };
    if (!Object.keys(patch).length) return;
    for (const k of Object.keys(patch)) delete pending[k];
    try {
      await api().set_settings(patch);
    } catch (e) {
      for (const [k, v] of Object.entries(patch)) if (!(k in pending)) pending[k] = v;
      throw e;
    }
  };
  settingWrite = settingWrite.catch(() => {}).then(write);
  return settingWrite;
}
function pushSetting(key, value) {
  pending[key] = value;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => flushSettings().catch(() => notify(ux('failed'), 'error')), 120);
}

/* ------------------------------------------------------------------ 交互 */
function switchTab(index) {
  document.querySelectorAll('.tab').forEach((tab) => {
    const selected = tab.dataset.tab === String(index);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  $('page0').classList.toggle('active', index === 0);
  $('page1').classList.toggle('active', index === 1);
  pushSetting('selected_tab', index);
}

/* 折叠面板。
 *
 * **窗口尺寸固定，不跟着面板走。** 以前展开时会算「全部折叠时的高度 + 面板高度」
 * 再调 set_window_height 把窗口撑高 —— 效果是点一下「高级设置」窗口就蹿到屏幕
 * 底部（在 965×728 的窗口上要长 200px），用户根本不想让窗口自己变。
 *
 * 现在面板就在原地展开，放不下的部分由 .page 自己滚：日志区是 ``flex: 1 1 0``
 * （基准 0），它会先让出空间；小到连 min-height 都不够时页面才出现滚动条。
 * 代价是矮窗口下要多滚一下，但窗口不会突然变形 —— 这个更烦人。
 */

/* 展开后把页面滚到「主操作按钮完整可见」为止。
 *
 * 不滚的话，窗口 728 高时主按钮正好卡在页面下边缘 —— 截图里就是一颗被切成
 * 两半的按钮，看着像渲染坏了。滚到底（delta 会被 maxScroll 夹住）之后，
 * 面板和按钮都在视野里，代价是最上面一两行表单滚出去，而用户刚点的就是
 * 最下面那块，这个取舍是划算的。
 *
 * 用「差多少滚多少」而不是 scrollIntoView('center')：用户刚点的面板标题
 * 不能从眼前跑掉。 */
function revealPanel(body) {
  const page = body.closest('.page');
  if (!page || page.scrollHeight <= page.clientHeight + 1) return;
  const action = page.querySelector('.action-primary') || body;
  const view = page.getBoundingClientRect();
  const maxTop = page.scrollHeight - page.clientHeight;
  const delta = Math.min(action.getBoundingClientRect().bottom - view.bottom + 10, maxTop);
  if (delta <= 0) return;
  const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  page.scrollTo({ top: Math.min(page.scrollTop + delta, maxTop),
                  behavior: smooth ? 'smooth' : 'auto' });
}

/* 等 max-height 过渡跑完再量，否则量到的是动画中间态 */
function afterTransition(el, fn) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    requestAnimationFrame(fn);
    return;
  }
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    el.removeEventListener('transitionend', onEnd);
    clearTimeout(timer);
    fn();
  };
  const onEnd = (e) => {
    if (e.target === el && e.propertyName === 'max-height') finish();
  };
  el.addEventListener('transitionend', onEnd);
  // 过渡被跳过（系统开了减少动效、或高度没变化不触发 transitionend）时的兜底
  const timer = setTimeout(finish, 400);
}

function toggleAdv(head, body) {
  const open = head.getAttribute('aria-expanded') !== 'true';

  head.setAttribute('aria-expanded', String(open));
  body.classList.toggle('open', open);
  body.inert = !open;
  body.setAttribute('aria-hidden', String(!open));

  if (open) afterTransition(body, () => revealPanel(body));
}

function bind() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchTab(Number(tab.dataset.tab)));
  });

  $('advSaiHead').addEventListener('click', () => toggleAdv($('advSaiHead'), $('advSaiBody')));
  $('advExportHead').addEventListener('click', () => toggleAdv($('advExportHead'), $('advExportBody')));

  // 文本 / 数字输入：change（失焦或回车）时提交，避免每敲一个字就落盘
  const textFields = {
    framesFile: 'frames_file',
    autoSplit: 'auto_split_sai',
    sizeValue: 'image_size_limit',
    recContainer: 'recording_custom_container',
    recCodec: 'recording_custom_codec',
    exportFps: 'export_fps',
    exportLimit: 'export_time_limit',
    previewDuration: 'export_preview_duration',
    expContainer: 'export_custom_container',
    expCodec: 'export_custom_codec',
  };
  for (const [id, key] of Object.entries(textFields)) {
    $(id).addEventListener('change', (e) => {
      let value = e.target.value;
      if (e.target.type === 'number') {
        const limits = {
          auto_split_sai: [0, 100000, 0],
          image_size_limit: [100, 16384, 1920],
          export_fps: [1, 120, 30],
          export_time_limit: [0, 86400, 0],
          export_preview_duration: [0, 60, 0],
        }[key];
        if (limits) {
          value = clamp(value, limits[0], limits[1], limits[2]);
          e.target.value = value;
        }
      }
      settings[key] = value;
      pushSetting(key, value);
      if (key === 'frames_file') {
        $('framesReadonly').textContent = value;
        $('framesReadonly').classList.toggle('empty', !value);
      }
      if (key === 'image_size_limit') syncSizePreset(Number(value));
    });
  }

  const selects = {
    recType: 'recording_type',
    expType: 'export_type',
    versionSel: 'sai_version_override',
    themeSel: 'theme',
    langSel: 'lang',
  };
  for (const [id, key] of Object.entries(selects)) {
    $(id).addEventListener('change', (e) => {
      const value = ['recording_type', 'export_type', 'sai_version_override'].includes(key)
        ? Number(e.target.value) : e.target.value;
      settings[key] = value;
      if (key === 'theme') applyTheme(value);
      pushSetting(key, value);
      if (key === 'recording_type' || key === 'export_type') renderSettings(true);
    });
  }

  $('canvasSel').addEventListener('change', (e) => {
    settings.sai_canvas = e.target.value;
    pushSetting('sai_canvas', e.target.value);
  });

  $('sizePreset').addEventListener('change', (e) => {
    // option 的 value 就是像素数（见 renderSettings 里建选项那段）
    const px = clamp(Number(e.target.value), 100, ORIGINAL_SIZE, 1920);
    settings.image_size_limit = px;
    $('sizeValue').value = px;
    pushSetting('image_size_limit', px);
  });

  $('previewFirst').addEventListener('change', (e) => {
    settings.export_preview_last_frame = e.target.checked;
    pushSetting('export_preview_last_frame', e.target.checked);
  });

  // 接着上次录制：关的时候每次开始都是新的一段，导出只读最新那个会话
  $('appendLast').addEventListener('change', (e) => {
    settings.append_last_session = e.target.checked;
    pushSetting('append_last_session', e.target.checked);
  });

  $('useRecType').addEventListener('change', (e) => {
    settings.export_user_recording = e.target.checked;
    pushSetting('export_user_recording', e.target.checked);
    renderSettings(true);
  });

  $('recBtn').addEventListener('click', () => {
    // 录制中点它 = 结束，先问一句。误点一下就把整段会话停掉的代价太大。
    if (status.op?.mode === 'record') {
      askStopRecording();
      return;
    }
    // 还没选保存位置：别让它默默录到程序目录里，直接把引导框拉出来
    if (!settings.frames_file) {
      openSetup();
      return;
    }
    runAction('recBtn', async () => { await flushSettings(); await api().toggle_record(); await poll(); });
  });

  $('pauseBtn').addEventListener('click', () => {
    runAction('pauseBtn', async () => {
      if (status.op?.paused) await api().resume_record();
      else await api().pause_record();
      await poll();
    });
  });

  function startExport() {
    runAction('expBtn', async () => { await flushSettings(); await api().toggle_export(); await poll(); });
  }

  $('expBtn').addEventListener('click', () => {
    if (!settings.frames_file) {
      openSetup();
      return;
    }
    /* 缩时是按「画面变化」记帧的，不是按时间录屏 —— 帧数少的时候成片只有
     * 零点几秒，播放器一闪而过，用户会以为程序坏了（2026-11-19 那位群友就是
     * 6 帧 / 50fps = 0.12 秒，专门拍了录屏来问）。这里把事实摆出来让他自己决定。 */
    const frames = Number(status.export?.frames || 0);
    if (frames > 0 && frames < MIN_EXPORT_FRAMES) {
      const fps = Math.max(1, Number(settings.export_fps) || 30);
      const seconds = Math.max(0.1, frames / fps).toFixed(1);
      ask(ux('fewTitle'),
          ux('fewBody').replace('{n}', frames).replace('{s}', seconds),
          ux('fewOk'), startExport, 'fewFrames');
      return;
    }
    startExport();
  });

  $('setupPick').addEventListener('click', async () => {
    // pick_folder 成功后后端已经写进设置，这里只需要同步本地那份
    await runAction('setupPick', async () => applySaveDir(await api().pick_folder(settings.frames_file || '')));
  });

  $('setupUse').addEventListener('click', async () => {
    await runAction('setupUse', async () => applySaveDir(await api().use_default_save_dir()));
  });

  $('confirmCancel').addEventListener('click', closeConfirm);
  $('confirmOk').addEventListener('click', () => {
    const run = confirmOnOk;
    closeConfirm();
    if (run) run();
  });
  // 点遮罩空白处 = 取消。点卡片本身不算（否则选文字都会误关）。
  $('confirmMask').addEventListener('click', (e) => {
    if (e.target === $('confirmMask')) closeConfirm();
  });

  $('pickFrames').addEventListener('click', async () => {
    await runAction('pickFrames', async () => {
      await flushSettings();
      const path = await api().pick_folder(settings.frames_file || '');
      if (path) {
        settings.frames_file = path;
        renderSettings(true);
      }
    });
  });

  $('openExport').addEventListener('click', () => {
    const path = status.export?.result;
    if (path) runAction('openExport', () => api().open_folder(path.replace(/[\\/][^\\/]*$/, '')));
  });

  // 归档刚导完的那批帧。后端只是把会话目录 move 进 archive/，不删文件。
  // 用 busyButtons 自己挡重入 —— runAction 会顺手改按钮文案，这里要保持原样。
  $('archiveFrames').addEventListener('click', async () => {
    const btn = $('archiveFrames');
    if (busyButtons.has('archiveFrames')) return;
    busyButtons.add('archiveFrames');
    btn.disabled = true;
    try {
      const res = await api().archive_frames();
      const moved = (res && res.moved) || 0;
      if (moved > 0) {
        notify(ux('archived').replace('{}', moved));
        btn.hidden = true;          // 这一支已经收起来了，别再点第二次
        // 帧数会自动归零：后端 touch_summary() 之后 poll 就会带回新的「本次录制」
      } else {
        notify(ux('archive_empty'), 'error');
      }
    } catch (e) {
      notify(ux('failed'), 'error');
    } finally {
      busyButtons.delete('archiveFrames');
      btn.disabled = false;
    }
  });

  $('logClear').addEventListener('click', async () => {
    await runAction('logClear', async () => {
      await api().clear_log();
      $('logBody').innerHTML = '';
      syncLogEmpty();
    }, 'cleared');
  });

  $('logCopy').addEventListener('click', async () => {
    await runAction('logCopy', async () => {
      const text = await api().copy_log();
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(ta);
        try {
          ta.select();
          if (!document.execCommand('copy')) throw new Error('Clipboard unavailable');
        } finally {
          ta.remove();
          $('logCopy').focus();
        }
      }
    }, 'copied');
  });

  $('logOpen').addEventListener('click', () => runAction('logOpen', () => api().open_log_file()));

  // 反馈群号：点一下复制。剪贴板 API 正常可用，但 WebView2 里权限被拒的话
  // 要能兜住 —— 降级方案和日志复制那段同一套。
  $('feedbackBtn').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(FEEDBACK_QQ);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = FEEDBACK_QQ;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0';
      document.body.appendChild(ta);
      let ok = false;
      try {
        ta.select();
        ok = document.execCommand('copy');
      } catch (err) {
        ok = false;
      } finally {
        ta.remove();
      }
      if (!ok) {
        notify(ux('failed'), 'error');
        return;
      }
    }
    notify(ux('fb_copied'));
    $('feedbackBtn').focus();
  });

  // 悬浮条开关。后端 set_settings 收到 floatbar 变化后会真的起停那条 Tk 线程。
  $('floatBtn').addEventListener('click', () => {
    const next = !floatOn();
    settings.floatbar = next;
    pushSetting('floatbar', next);
    renderFloatBtn();
  });

  $('restoreBtn').addEventListener('click', () => {
    ask(S.restore_defaults, S.confirm_restore, S.restore_defaults, () => runAction('restoreBtn', async () => {
      await flushSettings();
      await api().restore_defaults();
      await bootstrap();
    }, 'saved'), 'restore');
  });

  // Ctrl+Tab 切页签
  window.addEventListener('keydown', (e) => {
    const modal = document.querySelector('.modal-mask:not([hidden])');
    if (modal) {
      if (e.key === 'Escape' && modal.id === 'confirmMask') {
        e.preventDefault();
        closeConfirm();
      } else if (e.key === 'Tab') {
        const items = [...modal.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled)')].filter(el => el.getClientRects().length);
        if (items.length) {
          const first = items[0], last = items[items.length - 1];
          if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
        } else e.preventDefault();
      } else if (e.ctrlKey && e.key === 'Tab') e.preventDefault();
      return;
    }
    if (e.target instanceof Element && e.target.matches('.tab') &&
        ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      const index = e.key === 'Home' ? 0 : e.key === 'End' ? 1 : Number(e.target.dataset.tab) ^ 1;
      switchTab(index);
      $(`tab${index}`).focus();
      return;
    }
    if (e.ctrlKey && e.key === 'Tab') {
      e.preventDefault();
      switchTab($('page0').classList.contains('active') ? 1 : 0);
    }
  });
}

/* ------------------------------------------------------------------ 启动 */
window.addEventListener('pywebviewready', async () => {
  bind();
  await bootstrap();
  // 首帧渲染完再开始轮询，避免和 bootstrap 抢。
  // 150ms 一次跨进程 IPC，拖动窗口时 WebView2 的重绘和它叠在一起，肉眼就是
  // 一顿一顿的。录制时长按秒显示，250ms 完全够，少四成的 IPC。
  setInterval(poll, 250);
  watchResize();
});

/* 拖窗口边缘改变大小时，每帧都要按新尺寸重排重绘。纯装饰的东西（纹样 mask、
 * 卡片投影）在这一帧的临界路径上却毫无视觉收益，先摘掉，停手 180ms 再挂回去。
 *
 * 只挂/摘一个 class：JS 侧不做任何测量（读 offsetWidth 会强制同步布局，
 * 那才是真正的卡顿来源）。去抖用 setTimeout 而不是 rAF —— rAF 在拖动期间
 * 每帧都会回调，等于没去抖。 */
let resizeTimer = null;
function watchResize() {
  window.addEventListener('resize', () => {
    document.body.classList.add('is-resizing');
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      document.body.classList.remove('is-resizing');
    }, 180);
  }, { passive: true });
}

/* 截图/自检用的入口（ART_TIMELAPSE_UI_SCRIPT 里调）。
 * 必须等 bootstrap 跑完再挂出去 —— 否则外部脚本可能抢在 api 注入之前调，
 * 拿到的 api 是 undefined，报的错还完全不像「还没就绪」。 */
function exposeTestApi() {
  window.__app = {
    switchTab,
    openAdv(which) {
      const head = which === 'export' ? $('advExportHead') : $('advSaiHead');
      const body = which === 'export' ? $('advExportBody') : $('advSaiBody');
      if (head.getAttribute('aria-expanded') !== 'true') return toggleAdv(head, body);
      return Promise.resolve();
    },
    state: () => ({
      settings,
      status,
      // 探针用：rev 有没有递增 = 后端到底有没有重新抓图；
      // len 是当前 data URL 的长度，换了画布必然变。
      previewRev,
      previewLen: previewData ? previewData.length : 0,
      previewLabel: $('previewSize').textContent,
      // 面板展开时页面能不能滚得动（窗口尺寸固定，滚动是唯一出路）
      pageScrollTop: document.querySelector('.page.active')?.scrollTop || 0,
    }),
  };
}
