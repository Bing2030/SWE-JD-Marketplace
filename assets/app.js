/* SWE-JD 数据集站点：MiniSearch 全文搜索 + 多维 facets + 统计面板（零构建，纯静态） */
/* global MiniSearch */
'use strict';

// ---------------------------------------------------------------- 常量
const LEVEL_ORDER = ['campus', 'entry', 'mid', 'senior', 'lead', 'staff',
  'architect', 'manager', 'principal', 'director', 'cto', 'unknown'];
const LEVEL_ZH = { campus: '校招', entry: '入门', mid: '中级', senior: '高级',
  lead: '负责人', staff: '专家', architect: '架构师', manager: '经理',
  principal: '首席/Principal', director: '总监', cto: 'CTO', unknown: '未标注' };
const ROLE_ZH = { ai_agent: 'AI/Agent', ai_platform: 'AI 平台', architecture: '架构',
  backend: '后端/Java', management: '管理', payment_risk: '支付风控', other: '其他' };
const REGION_ZH = { cn: '中国', sg: '新加坡', us: '美国' };
const WORK_ZH = { onsite: '现场办公', remote: '远程', hybrid: '混合', unknown: '办公方式未标注' };
const SALARY_ORDER = ['<30万', '30–60万', '60–100万', '100万+', '美元·年薪', '月薪标注', '未标注'];

const FACETS = [
  { key: 'region', zh: '地区', val: j => [j.region], zhOf: REGION_ZH },
  { key: 'platform', zh: '平台', val: j => [primaryPlatform(j)] },
  { key: 'level', zh: '级别', val: j => [j.level], zhOf: LEVEL_ZH, order: LEVEL_ORDER },
  { key: 'role', zh: '角色', val: j => [j.role], zhOf: ROLE_ZH },
  { key: 'city', zh: '城市', val: j => j.cities || [], top: 12 },
  { key: 'salary', zh: '薪资档', val: j => [salaryBand(j)], order: SALARY_ORDER },
  { key: 'work', zh: '办公方式', val: j => [j.workplace], zhOf: WORK_ZH },
];

const PER_PAGE = 50;
const state = { q: '', f: {}, sort: 'recent', page: 1 };
let JOBS = [];
let MS = null;
let GLOB = {};            // key -> Map(value -> 全量计数)
let debounceTimer = null;

// ---------------------------------------------------------------- 工具
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const escAttr = esc;

const primaryPlatform = j => (j.sources && j.sources[0] ? j.sources[0].platform : 'unknown');

function salaryBand(j) {
  const s = j.salary;
  if (!s) return '未标注';
  if (s.currency === 'USD' && s.period === 'annual') return '美元·年薪';
  const ann = s.annual_min != null ? s.annual_min : (s.period === 'annual' ? s.min : null);
  if (ann != null) {
    if (ann < 300000) return '<30万';
    if (ann < 600000) return '30–60万';
    if (ann < 1000000) return '60–100万';
    return '100万+';
  }
  if (s.period === 'monthly') return '月薪标注';
  return '未标注';
}

// ---------------------------------------------------------------- 分词
// 文档侧：ASCII 按词；CJK 连续段滑窗 bigram（孤立单字保留）。
function tokenizeDoc(text) {
  const s = String(text || '').toLowerCase(), out = [];
  let m;
  const reA = /[a-z0-9]+/g, reC = /[一-鿿]+/g;
  while ((m = reA.exec(s))) out.push(m[0]);
  while ((m = reC.exec(s))) {
    const run = m[0];
    if (run.length === 1) out.push(run);
    else for (let i = 0; i + 1 < run.length; i++) out.push(run.slice(i, i + 2));
  }
  return out;
}

// 查询侧：字符串子查询走索引级 tokenizeDoc（语义一致）；
// CJK 单字包成 prefix 节点（前缀命中 bigram），ASCII 词精确（java 不误中 javascript）。
function buildQuery(q) {
  const s = String(q || '').trim().toLowerCase();
  if (!s) return null;
  const sub = [];
  let m;
  const reA = /[a-z0-9]+/g, reC = /[一-鿿]+/g;
  while ((m = reA.exec(s))) sub.push(m[0]);
  while ((m = reC.exec(s))) {
    const run = m[0];
    if (run.length === 1) sub.push({ queries: [run], prefix: true });
    else for (let i = 0; i + 1 < run.length; i++) sub.push(run.slice(i, i + 2));
  }
  return sub.length ? { combineWith: 'AND', queries: sub } : null;
}

function searchIds(q) {
  const tree = buildQuery(q);
  if (!tree) return null;
  const res = MS.search(tree, {
    boost: { title: 4, company: 3, skills: 3, notes: 2, cities: 1, description: 1 },
  });
  return new Set(res.map(r => r.id));
}

// ---------------------------------------------------------------- 筛选 / 排序
const sel = key => state.f[key];
const passFacets = (j, skipKey) => FACETS.every(f =>
  f.key === skipKey || !sel(f.key).size || f.val(j).some(v => sel(f.key).has(v)));

function sortList(list) {
  const by = state.sort;
  const cmp = {
    recent: (a, b) => (b.collected_at || '').localeCompare(a.collected_at || '') || a.id.localeCompare(b.id),
    title: (a, b) => a.title.localeCompare(b.title, 'zh') || a.id.localeCompare(b.id),
    company: (a, b) => a.company.localeCompare(b.company, 'zh') || a.title.localeCompare(b.title, 'zh'),
  }[by];
  return list.slice().sort(cmp);
}

// ---------------------------------------------------------------- 渲染
function cardHTML(j) {
  const badges = [`<span class="badge ${j.is_senior === true ? 'b-sen' : j.is_senior === false ? 'b-jun' : 'b-unk'}">${esc(LEVEL_ZH[j.level] || j.level)}</span>`];
  if (j.role && j.role !== 'other') badges.push(`<span class="chip">${esc(ROLE_ZH[j.role] || j.role)}</span>`);
  if (j.ai_related) badges.push('<span class="chip ai">AI 相关</span>');

  const meta = [ (j.cities || []).join(' / '), j.salary && j.salary.text, j.experience,
    j.applicants != null ? `${j.applicants} 人申请` : '',
    j.workplace && j.workplace !== 'unknown' ? WORK_ZH[j.workplace] : '',
    j.collected_at ].filter(Boolean).map(esc).join(' · ');

  const platTags = (j.sources || []).map(s =>
    `<a class="tag" href="${escAttr(s.url || '#')}" target="_blank" rel="noopener">${esc(s.platform)}</a>`).join('');
  const skillChips = (j.skills || []).slice(0, 14).map(s => `<span class="chip skill">${esc(s)}</span>`).join('');

  return `<article class="card" data-id="${escAttr(j.id)}">
    <div class="card-head">
      <div class="ht">
        <h3>${esc(j.title)}</h3>
        <span class="company">${esc(j.company)}</span>
      </div>
      <div class="badges">${badges.join('')}</div>
    </div>
    <div class="meta">${meta || '&nbsp;'}</div>
    ${skillChips ? `<div class="skills">${skillChips}</div>` : ''}
    <div class="body">
      ${j.description ? `<h4>职位描述</h4><pre class="jd">${esc(j.description)}</pre>` : ''}
      ${j.notes ? `<h4>分析备注</h4><pre class="jd notes">${esc(j.notes)}</pre>` : ''}
      ${platTags ? `<h4>来源</h4><div class="srcs">${platTags}</div>` : ''}
    </div>
    <button class="toggle" type="button">展开详情 ▾</button>
  </article>`;
}

function renderCards(list) {
  const shown = list.slice(0, state.page * PER_PAGE);
  document.getElementById('cards').innerHTML = shown.map(cardHTML).join('');
  document.getElementById('empty').hidden = shown.length > 0;
  document.getElementById('more').hidden = shown.length >= list.length;
  document.getElementById('more').textContent = `加载更多（${list.length - shown.length} 条剩余）`;
}

function facetValues(f) {
  const counts = GLOB[f.key];
  let vals = [...counts.entries()].filter(([, n]) => n > 0).map(([v]) => v);
  if (f.order) vals.sort((a, b) => f.order.indexOf(a) - f.order.indexOf(b));
  else vals.sort((a, b) => counts.get(b) - counts.get(a));
  if (f.top) vals = vals.slice(0, f.top);
  return vals;
}

function renderFacets(countsByKey) {
  const el = document.getElementById('facets');
  el.innerHTML = FACETS.map(f => {
    const rows = facetValues(f).map(v => {
      const n = (countsByKey[f.key] && countsByKey[f.key].get(v)) || 0;
      const label = f.zhOf ? (f.zhOf[v] || v) : v;
      return `<label class="fopt${n === 0 ? ' off' : ''}">
        <input type="checkbox" data-fk="${f.key}" value="${escAttr(v)}"${sel(f.key).has(v) ? ' checked' : ''}>
        <span class="fv">${esc(label)}</span><span class="fc">${n}</span></label>`;
    }).join('');
    return `<details class="fg" open><summary>${esc(f.zh)}</summary><div class="fopts">${rows}</div></details>`;
  }).join('');
}

function renderStats(c) {
  const total = c.total;
  const bar = (label, n) =>
    `<div class="srow"><span class="sl">${esc(label)}</span><span class="sb"><i style="width:${Math.max(2, n / total * 100).toFixed(1)}%"></i></span><span class="sn">${n}</span></div>`;
  const platCounts = {};
  JOBS.forEach(j => { const p = primaryPlatform(j); platCounts[p] = (platCounts[p] || 0) + 1; });
  const topPlat = Object.entries(platCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const withSalary = JOBS.filter(j => j.salary && j.salary.text).length;
  const is = c.is_senior || {};
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="sn2">${total}</div><div class="sl2">职位总数</div></div>
    <div class="stat"><div class="sn2">${c.ai_related}</div><div class="sl2">AI 相关</div></div>
    <div class="stat"><div class="sn2">${is['True'] || 0}</div><div class="sl2">资深及以上</div></div>
    <div class="stat"><div class="sn2">${withSalary}</div><div class="sl2">标注薪资</div></div>
    <div class="stat wide">
      ${bar(REGION_ZH.cn, c.region.cn || 0)}${bar(REGION_ZH.sg, c.region.sg || 0)}${bar(REGION_ZH.us, c.region.us || 0)}
    </div>
    <div class="stat wide">${topPlat.map(([p, n]) => bar(p, n)).join('')}</div>
    <div class="stat wide">
      ${bar('资深+', is['True'] || 0)}${bar('非资深', is['False'] || 0)}${bar('级别未知', is['None'] || is['null'] || 0)}
    </div>`;
}

// ---------------------------------------------------------------- 主流程
function run() {
  const idHits = state.q ? searchIds(state.q) : null;
  let list = JOBS.filter(j => passFacets(j));
  if (idHits) list = list.filter(j => idHits.has(j.id));
  list = sortList(list);

  // 每组 facet 的计数基：查询 + 其他组筛选（不含本组自身）
  const countsByKey = {};
  for (const f of FACETS) {
    const m = new Map();
    JOBS.forEach(j => {
      if (idHits && !idHits.has(j.id)) return;
      if (!passFacets(j, f.key)) return;
      f.val(j).forEach(v => { if (GLOB[f.key].has(v)) m.set(v, (m.get(v) || 0) + 1); });
    });
    countsByKey[f.key] = m;
  }

  renderFacets(countsByKey);
  renderCards(list);
  document.getElementById('count').textContent =
    `${list.length} / ${JOBS.length} 条${state.q ? ` · “${state.q}”` : ''}`;
  writeHash();
}

function computeGlob() {
  FACETS.forEach(f => {
    const m = new Map();
    JOBS.forEach(j => f.val(j).forEach(v => m.set(v, (m.get(v) || 0) + 1)));
    GLOB[f.key] = m;
  });
}

// ---------------------------------------------------------------- hash 状态
function writeHash() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  if (state.sort !== 'recent') p.set('sort', state.sort);
  if (state.page > 1) p.set('p', state.page);
  FACETS.forEach(f => { const v = [...sel(f.key)]; if (v.length) p.set(f.key, v.join(',')); });
  const h = p.toString();
  history.replaceState(null, '', h ? '#' + h : location.pathname + location.search);
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  state.q = p.get('q') || '';
  state.sort = p.get('sort') || 'recent';
  state.page = Math.max(1, parseInt(p.get('p') || '1', 10) || 1);
  FACETS.forEach(f => {
    const v = p.get(f.key);
    state.f[f.key] = new Set(v ? v.split(',').filter(x => GLOB[f.key].has(x)) : []);
  });
  document.getElementById('q').value = state.q;
  document.getElementById('sort').value = state.sort;
}

// ---------------------------------------------------------------- 事件与启动
function bind() {
  const qEl = document.getElementById('q');
  qEl.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => { state.q = qEl.value.trim(); state.page = 1; run(); }, 150);
  });
  document.getElementById('sort').addEventListener('change', e => { state.sort = e.target.value; run(); });
  document.getElementById('facets').addEventListener('change', e => {
    const cb = e.target;
    if (cb.tagName !== 'INPUT') return;
    const set = sel(cb.dataset.fk);
    if (cb.checked) set.add(cb.value); else set.delete(cb.value);
    state.page = 1;
    run();
  });
  document.getElementById('more').addEventListener('click', () => { state.page++; run(); });
  document.getElementById('cards').addEventListener('click', e => {
    const btn = e.target.closest('.toggle');
    if (!btn) return;
    const card = btn.closest('.card');
    const open = card.classList.toggle('open');
    btn.textContent = open ? '收起 ▴' : '展开详情 ▾';
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== qEl && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      e.preventDefault(); qEl.focus(); qEl.select();
    }
    if (e.key === 'Escape' && document.activeElement === qEl) {
      qEl.value = ''; state.q = ''; state.page = 1; run(); qEl.blur();
    }
  });
}

async function init() {
  try {
    if (typeof MiniSearch === 'undefined') throw new Error('MiniSearch 未加载（assets/vendor/minisearch.js）');
    const res = await fetch('data/jobs.json');
    if (!res.ok) throw new Error(`data/jobs.json ${res.status}`);
    const data = await res.json();
    JOBS = data.jobs;

    MS = new MiniSearch({
      fields: ['title', 'company', 'skills', 'cities', 'description', 'notes'],
      tokenize: tokenizeDoc,
    });
    MS.addAll(JOBS.map(j => ({
      id: j.id, title: j.title, company: j.company,
      skills: (j.skills || []).join(' '), cities: (j.cities || []).join(' '),
      description: j.description || '', notes: j.notes || '',
    })));

    computeGlob();
    renderStats(data.meta.counts);
    readHash();
    bind();
    run();
  } catch (err) {
    document.getElementById('count').textContent = '加载失败';
    document.getElementById('cards').innerHTML =
      `<p class="empty">数据加载失败：${esc(err.message)}</p>`;
    console.error(err);
  }
}

document.addEventListener('DOMContentLoaded', init);
