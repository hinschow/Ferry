// Ferry 网页界面。所有逻辑在 agent（ferry_core.py），这里只负责画和转发操作。
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c;
                          if (x !== undefined) e.textContent = x; return e; };
const esc = s => String(s == null ? '' : s);

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Ferry-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

let ST = { servers: [], sshd: {} };
let ACTIVE = null;
const OPEN = new Set();          // 展开了详情的服务器
let LOGSEQ = 0;

// ───────────────────────── 渲染

function dotClass(s) { return s && s.ok ? 'ok' : (s && s.ok === null ? '' : 'bad'); }

function renderServers() {
  const box = $('#srvlist');
  box.textContent = '';
  if (!ST.servers.length) {
    const e = el('div', 'empty', '还没有服务器');
    e.style.padding = '24px 12px';
    box.appendChild(e);
    return;
  }
  for (const s of ST.servers) {
    const on = s.id === ACTIVE;
    const card = el('div', 'srv' + (on ? ' on' : '') + (OPEN.has(s.id) ? ' open' : ''));
    card.onclick = ev => { if (!ev.target.closest('button')) select(s.id); };

    const top = el('div', 'top');
    top.appendChild(el('span', 'dot ' + dotClass(s.tunnel)));
    const nm = el('div', 'nm', s.name); top.appendChild(nm);

    const conn = el('button', 'mini' + (s.tunnel.ok ? '' : ' pri'),
                    s.tunnel.ok ? '断开' : '连接');
    conn.onclick = () => api(s.tunnel.ok ? '/api/tunnel/stop' : '/api/tunnel/start', { id: s.id });
    top.appendChild(conn);

    const caret = el('button', 'mini', OPEN.has(s.id) ? '▾' : '▸');
    caret.onclick = () => { OPEN.has(s.id) ? OPEN.delete(s.id) : OPEN.add(s.id); renderServers(); };
    top.appendChild(caret);
    card.appendChild(top);

    const n = s.mounts.filter(m => m.mounted).length;
    card.appendChild(el('div', 'sub',
      `${s.alias}${s.port ? ' :' + s.port : ''} · ${n ? n + ' 个挂载' : '未挂载'}`));

    const det = el('div', 'det');
    const kv = (k, v, cls) => {
      const r = el('div', 'kv'); r.appendChild(el('i', '', k));
      const val = el('span', cls || '', v); r.appendChild(val); det.appendChild(r);
    };
    kv('隧道', s.tunnel.text || '—', s.tunnel.ok ? '' : 'warnc');
    kv('服务器', s.server.text || '—');
    let pipe = s.source === 'local' && s.fresh_s != null
      ? `实时同步 · ${Math.round(s.fresh_s)}s 前`
      : s.source === 'ssh' ? 'SSH 探测（管道未就绪）'
      : s.source === 'stale' ? '等待状态…' : '—';
    if (!s.pipe_ok && s.port) pipe += ' · 管道未挂载';
    kv('状态', pipe);
    const acts = el('div', 'acts');
    const mk = (t, fn, cls) => { const b = el('button', 'mini' + (cls || ''), t); b.onclick = fn; acts.appendChild(b); };
    mk('重连', () => api('/api/tunnel/start', { id: s.id }));
    mk('移除', async () => {
      if (!confirm(`从列表移除 ${s.name}？（不会改动服务器本身）`)) return;
      try { await api('/api/server/remove', { id: s.id }); }
      catch (e) { alert(e.message); }
    });
    det.appendChild(acts);
    card.appendChild(det);
    box.appendChild(card);
  }
}

function renderMounts() {
  const s = ST.servers.find(x => x.id === ACTIVE);
  $('#mount-of').textContent = s ? '· ' + s.name : '';
  const box = $('#mounts');
  box.textContent = '';
  if (!s) {
    box.appendChild(el('div', 'empty', '左侧添加一台服务器后，就能把本机目录挂上去'));
    return;
  }
  for (const m of s.mounts) {
    const row = el('div', 'row');
    const tile = el('div', 'tile' + (m.mounted ? '' : ' off'));
    tile.innerHTML = '<svg><use href="#i-folder"/></svg>';
    row.appendChild(tile);

    const info = el('div', 'info');
    info.appendChild(el('div', 't', m.local.split(/[\\/]/).filter(Boolean).pop() || m.local));
    const p = el('div', 'p');
    p.appendChild(el('span', '', m.local));
    const a = el('span', 'arrow', '  →  '); p.appendChild(a);
    p.appendChild(el('span', '', m.server));
    info.appendChild(p);
    row.appendChild(info);

    row.appendChild(el('span', 'badge' + (m.mounted ? '' : ' off'),
                       m.mounted ? '已挂载' : '未挂载'));

    const bs = el('div', 'rowbtns');
    const mk = (t, fn) => { const b = el('button', 'mini', t); b.onclick = fn; bs.appendChild(b); };
    mk(m.mounted ? '卸载' : '挂载', () =>
      api(m.mounted ? '/api/umount' : '/api/mount',
          { id: s.id, local: m.local, server: m.server }).catch(e => alert(e.message)));
    if (!m.mounted) {
      mk('更改位置', () => mountDialog(s, m.local, m.server));
      mk('移除', () => api('/api/mount/remove', { id: s.id, local: m.local })
                        .catch(e => alert(e.message)));
    }
    row.appendChild(bs);
    box.appendChild(row);
  }
  const add = el('div', 'addrow', '＋ 挂载文件夹');
  add.onclick = () => mountDialog(s, '', '');
  box.appendChild(add);
}

function renderTop() {
  const d = $('#sshd-dot'), t = $('#sshd-txt');
  d.className = 'dot ' + dotClass(ST.sshd);
  t.textContent = ST.sshd.text || '—';
  $('#sshd-start').style.display = ST.sshd.ok === false ? '' : 'none';
  const s = ST.servers.find(x => x.id === ACTIVE);
  $('#uptime').textContent = s && s.uptime ? '服务器已运行 ' + s.uptime : '';
}

// ───────────────────────── 弹层

function modal(title, bodyNodes, buttons) {
  const m = $('#modal');
  m.textContent = '';
  m.appendChild(el('h3', '', title));
  const bd = el('div', 'bd');
  bodyNodes.forEach(n => bd.appendChild(n));
  m.appendChild(bd);
  const ft = el('div', 'ft');
  for (const [label, cls, fn] of buttons) {
    const b = el('button', 'btn ' + cls, label);
    b.onclick = fn;
    ft.appendChild(b);
  }
  m.appendChild(ft);
  $('#mask').classList.add('on');
}
const closeModal = () => $('#mask').classList.remove('on');

function field(labelText, value, hint, withBrowse) {
  const w = el('div');
  w.appendChild(el('label', '', labelText));
  const line = el('div', 'inline');
  const i = el('input');
  i.value = value || '';
  line.appendChild(i);
  if (withBrowse) {
    const b = el('button', 'btn', '浏览…');
    b.onclick = () => withBrowse(i);
    line.appendChild(b);
  }
  w.appendChild(line);
  if (hint) w.appendChild(el('div', 'hint', hint));
  w.input = i;
  return w;
}

function mountDialog(s, local, server) {
  let touched = !!server;
  const f1 = field('本机目录', local, '要挂到服务器上的本机文件夹', async inp => {
    const r = await api('/api/pick-folder');
    if (r.path) { inp.value = r.path; sync(); }
  });
  const f2 = field('服务器位置', server, '', inp => browseDialog(s, inp));
  f2.input.oninput = () => { touched = true; };
  const def = () => {
    const v = f1.input.value.trim();
    if (!v) return '';
    const name = v.replace(/[\\/: ]+/g, '_').replace(/^_|_$/g, '');
    return `${s.mnt_root}/${name}`;
  };
  const sync = () => { if (!touched) f2.input.value = def(); };
  f1.input.oninput = sync;
  if (!server) sync();

  modal(local ? '更改挂载位置' : '挂载文件夹', [f1, f2], [
    ['恢复默认位置', '', () => { touched = false; f2.input.value = def(); }],
    ['取消', '', closeModal],
    ['挂载', 'pri', async () => {
      const lv = f1.input.value.trim(), sv = f2.input.value.trim();
      if (!lv) return alert('先选一个本机目录');
      if (sv && !sv.startsWith('/')) return alert('服务器位置必须是绝对路径');
      closeModal();
      try { await api('/api/mount', { id: s.id, local: lv, server: sv }); }
      catch (e) { alert(e.message); }
    }],
  ]);
}

async function browseDialog(s, target) {
  let cur = target.value.replace(/\/[^/]*$/, '') || s.mnt_root;
  const list = el('div'); list.id = 'picker';
  const path = field('服务器位置', cur, '双击进入下一层；「已挂载」的目录不能重复占用');
  const load = async p => {
    list.textContent = '读取中…';
    try {
      const r = await api(`/api/browse?id=${encodeURIComponent(s.id)}&path=${encodeURIComponent(p)}`);
      cur = r.cwd; path.input.value = r.cwd;
      list.textContent = '';
      const up = el('div', '', '..  上一层');
      up.onclick = () => load(cur.replace(/\/[^/]+\/?$/, '') || '/');
      list.appendChild(up);
      for (const d of r.dirs) {
        const it = el('div', d.flag === 'mounted' ? 'taken' : '',
                      d.name + (d.flag === 'mounted' ? '   〔已挂载〕' : ''));
        it.onclick = () => load((cur === '/' ? '' : cur) + '/' + d.name);
        list.appendChild(it);
      }
      if (!r.dirs.length) list.appendChild(el('div', 'taken', '（空目录）'));
    } catch (e) { list.textContent = e.message; }
  };
  modal('选择服务器上的位置', [path, list], [
    ['取消', '', closeModal],
    ['选定当前目录', 'pri', () => { target.value = path.input.value.trim(); closeModal(); }],
  ]);
  load(cur);
}

function inviteDialog() {
  const w = el('div');
  w.appendChild(el('label', '', '把服务器上 bridge-invite 打印的接入码整段粘进来'));
  const ta = el('textarea');
  ta.placeholder = 'FERRY1:...';
  w.appendChild(ta);
  w.appendChild(el('div', 'hint', '⚠️ 接入码包含服务器登录凭据，别外传'));
  modal('添加服务器', [w], [
    ['取消', '', closeModal],
    ['接入', 'pri', async () => {
      const t = ta.value.trim();
      if (!t.startsWith('FERRY1:')) return alert('接入码应以 FERRY1: 开头');
      closeModal();
      try { await api('/api/server/add-invite', { token: t }); }
      catch (e) { alert(e.message); }
    }],
  ]);
  setTimeout(() => ta.focus(), 30);
}

// ───────────────────────── 轮询

function select(id) {
  ACTIVE = id;
  api('/api/select', { id });
  renderServers(); renderMounts(); renderTop();
}

async function tick() {
  try {
    const s = await api('/api/state');
    const changed = JSON.stringify(s) !== JSON.stringify(ST);
    ST = s;
    if (ACTIVE === null || !ST.servers.some(x => x.id === ACTIVE))
      ACTIVE = ST.active && ST.servers.some(x => x.id === ST.active)
               ? ST.active : (ST.servers[0] || {}).id || null;
    if (changed) { renderServers(); renderMounts(); }
    renderTop();
  } catch (e) { /* agent 还没起来或已退出 */ }
}

async function pullLog() {
  try {
    const r = await api('/api/log?since=' + LOGSEQ);
    const box = $('#log');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    for (const l of r.lines) {
      LOGSEQ = l.seq;
      const line = el('div');
      line.appendChild(el('span', 'ts', l.ts));
      line.appendChild(el('span', l.level, l.msg));
      box.appendChild(line);
    }
    while (box.childElementCount > 400) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { /* ignore */ }
}

$('#add-srv').onclick = inviteDialog;
$('#sshd-start').onclick = () => api('/api/sshd/start', {});
$('#panic').onclick = () => {
  if (confirm('将停止全部隧道并关闭本机 SSH 服务。\n所有服务器会立即失去对本机的访问权。\n\n确定继续？'))
    api('/api/panic', {});
};
$('#logbar').onclick = () => {
  const b = $('#log');
  b.classList.toggle('hide');
  $('#log-caret').textContent = b.classList.contains('hide') ? '▸' : '▾';
};
$('#mask').onclick = e => { if (e.target.id === 'mask') closeModal(); };
document.onkeydown = e => { if (e.key === 'Escape') closeModal(); };

tick(); pullLog();
setInterval(tick, 1000);
setInterval(pullLog, 1000);
