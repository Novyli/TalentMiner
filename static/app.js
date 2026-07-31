window.addEventListener("error", function(e) { console.error("TalentMiner error:", e.message, e.filename, e.lineno); });
const API = '';
const state = {
  authors: [],
  profiles: [],
  graph: null,
  selectedAuthor: null,
  taskId: null,
  pollTimer: null,
  graphSearch: { type: 'person', query: '', onlyMatches: false }
};
const svg = d3.select('#graph-svg');
const container = document.getElementById('graph-container');
const linkTooltip = document.getElementById('graph-link-tooltip');
const RELATION_META = {
  'co-author': {
    label: '合作关系',
    description: 'OpenAlex 记录显示两人曾共同署名论文',
    color: '#6c8cff',
    order: 0
  },
  'same-affiliation': {
    label: '同机构',
    description: '两人的机构信息中存在相同机构',
    color: '#4ade80',
    order: 1
  },
  'shared-topic': {
    label: '同领域',
    description: '两人的研究领域中存在相同主题',
    color: '#fb923c',
    order: 2
  }
};
let zoom, g;

function initSVG() {
  svg.selectAll('*').remove();
  const w = container.clientWidth, h = container.clientHeight;
  svg.attr('viewBox', `0 0 ${w} ${h}`).attr('width', w).attr('height', h);
  zoom = d3.zoom().scaleExtent([0.15, 6]).on('zoom', (e) => g.attr('transform', e.transform));
  svg.call(zoom);
  g = svg.append('g');
  svg.on('dblclick.zoom', null);
}

if (typeof d3 !== "undefined") { initSVG(); } else { console.error("D3 not loaded"); }
window.addEventListener('resize', () => { initSVG(); if (state.graph) renderGraph(state.graph); });

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body && !(body instanceof FormData)) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  else if (body instanceof FormData) { opts.body = body; }
  const res = await fetch(API + path, opts);
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || res.statusText); }
  return res.json();
}

function setStatus(text, cls) {
  const el = document.getElementById('status-indicator');
  el.textContent = text; el.className = cls;
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});

// PDF upload
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && file.name.toLowerCase().endsWith('.pdf')) handlePDFUpload(file);
});
fileInput.addEventListener('change', () => { if (fileInput.files[0]) handlePDFUpload(fileInput.files[0]); });

async function handlePDFUpload(file) {
  setStatus('解析中...', 'status-running');
  const form = new FormData(); form.append('file', file);
  try {
    const data = await api('POST', '/api/upload-pdf', form);
    state.context = data.context || {};
    renderAuthorList(data.authors);
    document.getElementById('upload-progress').style.display = 'none';
    setStatus('解析完成', 'status-done');
  } catch (e) {
    setStatus('解析失败', 'status-error');
    alert('PDF 解析失败: ' + e.message);
  }
}

// DOI search
document.getElementById('doi-search-btn').addEventListener('click', async () => {
  const doi = document.getElementById('doi-input').value.trim();
  if (!doi) return;
  setStatus('检索中...', 'status-running');
  try {
    const data = await api('POST', '/api/search-doi', { doi });
    state.context = data.context || {};
    renderAuthorList(data.authors);
    setStatus('检索完成', 'status-done');
  } catch (e) {
    setStatus('检索失败', 'status-error');
    alert('DOI 检索失败: ' + e.message);
  }
});

// Text analysis
document.getElementById('text-analyze-btn').addEventListener('click', async () => {
  const text = document.getElementById('text-input').value.trim();
  if (!text) return;
  try {
    const data = await api('POST', '/api/analyze-text', { text });
    state.context = data.context || {};
    renderAuthorList(data.authors);
  } catch (e) { alert('文本分析失败: ' + e.message); }
});

function renderAuthorList(authors) {
  state.authors = authors;
  const list = document.getElementById('author-list');
  list.innerHTML = authors.map((a, i) =>
    `<div class="author-item selected">
      <input type="checkbox" checked id="auth-${i}" value="${a}">
      <label for="auth-${i}">${a}</label>
    </div>`
  ).join('');
  document.getElementById('author-count').textContent = authors.length;
  document.getElementById('author-list-container').style.display = 'block';
  document.getElementById('crawl-action-panel').style.display = 'block';
  document.getElementById('start-crawl-btn').disabled = false;
}
document.getElementById('toggle-all').addEventListener('click', () => {
  const all = document.querySelectorAll('#author-list input[type="checkbox"]');
  const anyUnchecked = Array.from(all).some(c => !c.checked);
  all.forEach(c => { c.checked = anyUnchecked; c.closest('.author-item').classList.toggle('selected', c.checked); });
});

// Start crawl
document.getElementById('start-crawl-btn').addEventListener('click', async () => {
  const checked = Array.from(document.querySelectorAll('#author-list input:checked')).map(c => c.value);
  if (checked.length === 0) { alert('请选择至少一位作者'); return; }
  state.taskId = 'task-' + Date.now();
  setStatus('爬取中...', 'status-running');
  document.getElementById('progress-panel').style.display = 'block';
  document.getElementById('crawl-log').innerHTML = '';
  document.getElementById('start-crawl-btn').disabled = true;
  logCrawl(`开始爬取 ${checked.length} 位作者的信息...`);
  try {
    await api('POST', '/api/crawl-authors', { authors: checked, task_id: state.taskId, context: state.context || {} });
    state.pollTimer = setInterval(pollTask, 2000);
  } catch (e) {
    setStatus('爬取失败', 'status-error');
    logCrawl(`错误: ${e.message}`, 'log-error');
    document.getElementById('start-crawl-btn').disabled = false;
  }
});

async function pollTask() {
  try {
    const data = await api('GET', '/api/task-status/' + state.taskId);
    const pct = data.total ? Math.round(data.progress / data.total * 100) : 0;
    document.getElementById('crawl-progress-fill').style.width = pct + '%';
    document.getElementById('progress-text').textContent = `已完成 ${data.progress}/${data.total}`;
    if (data.results?.length) logCrawl(`已获取 ${data.results.length} 条人物档案`);
    if (data.status === 'completed') {
      clearInterval(state.pollTimer);
      state.profiles = data.results;
      setStatus('爬取完成', 'status-done');
      document.getElementById('start-crawl-btn').disabled = false;
      logCrawl(`爬取完成！共获取 ${data.results.length} 条人物档案`, 'log-success');
      await buildAndRenderGraph();
    } else if (data.status === 'error') {
      clearInterval(state.pollTimer);
      setStatus('爬取失败', 'status-error');
      logCrawl(`错误: ${data.error}`, 'log-error');
      document.getElementById('start-crawl-btn').disabled = false;
    }
  } catch (e) { logCrawl(`轮询错误: ${e.message}`, 'log-error'); }
}

function logCrawl(msg, cls = '') {
  const log = document.getElementById('crawl-log');
  log.innerHTML += `<div class="log-entry ${cls}">${new Date().toLocaleTimeString()} ${msg}</div>`;
  log.scrollTop = log.scrollHeight;
}

async function buildAndRenderGraph(projectId = state.taskId) {
  try {
    const graph = await api('POST', '/api/build-graph', {
      profiles: state.profiles,
      project_id: projectId
    });
    state.graph = graph;
    renderGraph(graph);
    renderStats(graph.stats);
    document.getElementById('empty-state').style.display = 'none';
    document.getElementById('network-stats').style.display = 'block';
  } catch (e) { console.error('Build graph failed:', e); }
}

function renderStats(stats) {
  document.getElementById('stats-content').innerHTML = `
    <div class="stat-row"><span>节点</span><span class="val">${stats.node_count || 0}</span></div>
    <div class="stat-row"><span>关系</span><span class="val">${stats.edge_count || 0}</span></div>
    <div class="stat-row"><span>人物对</span><span class="val">${stats.pair_count ?? stats.edge_count ?? 0}</span></div>
    <div class="stat-row"><span>密度</span><span class="val">${stats.density || 0}</span></div>
    <div class="stat-row"><span>平均度</span><span class="val">${stats.avg_degree || 0}</span></div>
    <div class="stat-row"><span>最大度</span><span class="val">${stats.max_degree || 0}</span></div>
  `;
}

function endpointId(value) {
  return typeof value === 'object' ? value.id : value;
}

function assignParallelOffsets(links) {
  const groups = new Map();
  links.forEach(link => {
    link._sourceId = endpointId(link.source);
    link._targetId = endpointId(link.target);
    const key = [link._sourceId, link._targetId].sort().join('\u0000');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(link);
  });
  groups.forEach(group => {
    group.sort((a, b) =>
      (RELATION_META[a.relation]?.order ?? 99) -
      (RELATION_META[b.relation]?.order ?? 99));
    group.forEach((link, index) => {
      const centeredIndex = index - (group.length - 1) / 2;
      const direction = link._sourceId.localeCompare(link._targetId) <= 0 ? 1 : -1;
      link._curveOffset = centeredIndex * 14 * direction;
    });
  });
}

function graphLinkPath(link) {
  const sx = link.source.x, sy = link.source.y;
  const tx = link.target.x, ty = link.target.y;
  const dx = tx - sx, dy = ty - sy;
  const length = Math.sqrt(dx * dx + dy * dy) || 1;
  const offset = link._curveOffset || 0;
  if (Math.abs(offset) < 0.1) return `M${sx},${sy} L${tx},${ty}`;
  const mx = (sx + tx) / 2 - dy / length * offset;
  const my = (sy + ty) / 2 + dx / length * offset;
  return `M${sx},${sy} Q${mx},${my} ${tx},${ty}`;
}

function positionLinkTooltip(event) {
  if (!linkTooltip) return;
  const rect = container.getBoundingClientRect();
  const maxLeft = Math.max(8, rect.width - linkTooltip.offsetWidth - 8);
  const maxTop = Math.max(8, rect.height - linkTooltip.offsetHeight - 8);
  linkTooltip.style.left = `${Math.max(8, Math.min(event.clientX - rect.left + 14, maxLeft))}px`;
  linkTooltip.style.top = `${Math.max(8, Math.min(event.clientY - rect.top + 14, maxTop))}px`;
}

function localizedLinkEvidence(link) {
  const evidenceItems = (link.evidence_items || []).filter(Boolean);
  if (evidenceItems.length) return evidenceItems.join('；');
  const evidence = link.evidence || '';
  if (evidence.startsWith('Both affiliated with: ')) {
    return `共同机构：${evidence.slice('Both affiliated with: '.length)}`;
  }
  if (evidence.startsWith('Both work on: ')) {
    return `共同研究领域：${evidence.slice('Both work on: '.length)}`;
  }
  if (evidence === 'Co-authorship found via OpenAlex') {
    return 'OpenAlex 共同署名记录';
  }
  return evidence;
}

function showLinkTooltip(event, link) {
  if (!linkTooltip) return;
  const meta = RELATION_META[link.relation] || {
    label: '其他关系',
    description: '系统记录的其他人物关系',
    color: '#9ca3af'
  };
  const evidence = localizedLinkEvidence(link);
  linkTooltip.replaceChildren();
  const title = document.createElement('div');
  title.className = 'tooltip-title';
  title.textContent = `${link.source.name || link._sourceId} ↔ ${link.target.name || link._targetId}`;
  const relation = document.createElement('div');
  relation.className = 'tooltip-relation';
  relation.style.color = meta.color;
  relation.textContent = meta.label;
  const description = document.createElement('div');
  description.className = 'tooltip-description';
  description.textContent = meta.description;
  linkTooltip.append(title, relation, description);
  if (evidence) {
    const evidenceEl = document.createElement('div');
    evidenceEl.className = 'tooltip-evidence';
    evidenceEl.textContent = evidence;
    linkTooltip.append(evidenceEl);
  }
  linkTooltip.style.display = 'block';
  linkTooltip.setAttribute('aria-hidden', 'false');
  positionLinkTooltip(event);
}

function linkTooltipText(link) {
  const meta = RELATION_META[link.relation] || {
    label: '其他关系',
    description: '系统记录的其他人物关系'
  };
  const evidence = localizedLinkEvidence(link);
  return [
    `${link.source.name || link._sourceId} ↔ ${link.target.name || link._targetId}`,
    meta.label,
    evidence || meta.description
  ].join('\n');
}

function hideLinkTooltip() {
  if (!linkTooltip) return;
  linkTooltip.style.display = 'none';
  linkTooltip.setAttribute('aria-hidden', 'true');
}

function renderGraph(data) {
  const w = container.clientWidth, h = container.clientHeight;
  hideLinkTooltip();
  g.selectAll('*').remove();

  // D3 mutates link.source/link.target into node objects. Always work on
  // fresh copies so the cached graph remains reusable after reload/filtering.
  const nodeDataAll = (data.nodes || []).map(n => ({ ...n }));
  let linkData = (data.edges || []).map(e => ({
    ...e,
    source: typeof e.source === 'object' ? e.source.id : e.source,
    target: typeof e.target === 'object' ? e.target.id : e.target
  })).filter(e => {
    if (!document.getElementById('show-coauthor').checked && e.relation === 'co-author') return false;
    if (!document.getElementById('show-affiliation').checked && e.relation === 'same-affiliation') return false;
    if (!document.getElementById('show-topic').checked && e.relation === 'shared-topic') return false;
    return true;
  });
  const linkedNodes = new Set();
  linkData.forEach(l => { linkedNodes.add(l.source); linkedNodes.add(l.target); });
  let nodeData = nodeDataAll.filter(n => linkedNodes.has(n.id) || n.group === 0);

  const searchQuery = (state.graphSearch.query || '').trim().toLocaleLowerCase();
  const hasSearch = searchQuery.length > 0;
  const matchedIds = new Set();
  if (hasSearch) {
    nodeData.forEach(n => {
      const isMatch = state.graphSearch.type === 'institution'
        ? n.group === 0 && (n.affiliations || []).some(a =>
            String(a).toLocaleLowerCase().includes(searchQuery))
        : String(n.name || '').toLocaleLowerCase().includes(searchQuery);
      if (isMatch) matchedIds.add(n.id);
    });
  }

  if (hasSearch && state.graphSearch.onlyMatches) {
    nodeData = nodeData.filter(n => matchedIds.has(n.id));
    linkData = linkData.filter(e =>
      matchedIds.has(e.source) && matchedIds.has(e.target));
  }

  const visibleIds = new Set(nodeData.map(n => n.id));
  linkData = linkData.filter(e =>
    visibleIds.has(e.source) && visibleIds.has(e.target));
  assignParallelOffsets(linkData);

  if (hasSearch) {
    let matchIndex = 0;
    nodeData.forEach(n => {
      if (matchedIds.has(n.id)) {
        const angle = matchIndex * 0.9;
        const radius = Math.min(matchIndex * 8, 60);
        n.x = w / 2 + Math.cos(angle) * radius;
        n.y = h / 2 + Math.sin(angle) * radius;
        matchIndex += 1;
      }
    });
  }

  const countEl = document.getElementById('graph-search-count');
  if (countEl) countEl.textContent = hasSearch ? `匹配 ${matchedIds.size} 人` : '';

  const sim = d3.forceSimulation(nodeData)
    .force('link', d3.forceLink(linkData).id(d => d.id).distance(80).strength(0.3))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(w / 2, h / 2))
    .force('collision', d3.forceCollide(16).strength(0.5));
  if (hasSearch) {
    sim
      .force('search-x', d3.forceX(w / 2).strength(d => matchedIds.has(d.id) ? 0.65 : 0.01))
      .force('search-y', d3.forceY(h / 2).strength(d => matchedIds.has(d.id) ? 0.65 : 0.01));
  }

  const linkGroup = g.append('g').selectAll('g').data(linkData).join('g')
    .attr('class', d => {
      if (!hasSearch || state.graphSearch.onlyMatches) return 'graph-link-group';
      return matchedIds.has(endpointId(d.source)) || matchedIds.has(endpointId(d.target))
        ? 'graph-link-group search-related'
        : 'graph-link-group search-dim';
    })
    .attr('data-relation', d => d.relation);

  const link = linkGroup.append('path')
    .attr('class', 'graph-link')
    .attr('stroke', d => RELATION_META[d.relation]?.color || '#555')
    .attr('stroke-width', d => Math.max(0.5, Math.min(3, d.weight * 1.5)))
    .attr('stroke-dasharray', d => d.relation === 'co-author' ? 'none' : '3,3');

  const linkHit = linkGroup.append('path')
    .attr('class', 'graph-link-hit')
    .on('pointerenter', function(event, d) {
      d3.select(this.parentNode).select('.graph-link').classed('is-hovered', true);
      showLinkTooltip(event, d);
    })
    .on('pointermove', positionLinkTooltip)
    .on('pointerleave', function() {
      d3.select(this.parentNode).select('.graph-link').classed('is-hovered', false);
      hideLinkTooltip();
    });
  linkHit.append('title').text(linkTooltipText);

  const node = g.append('g').selectAll('g').data(nodeData).join('g')
    .attr('class', d => {
      if (!hasSearch) return 'graph-node';
      return matchedIds.has(d.id)
        ? 'graph-node search-match'
        : 'graph-node search-dim';
    })
    .call(d3.drag().on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));

  node.append('circle')
    .attr('r', d => d.group === 0 ? 8 : 5)
    .attr('fill', d => hasSearch && matchedIds.has(d.id) ? '#facc15' : (d.group === 0 ? '#6c8cff' : '#a78bfa'))
    .attr('stroke', d => hasSearch && matchedIds.has(d.id) ? '#fff3a3' : (d.group === 0 ? '#8ba3ff' : '#c4b5fd'))
    .on('mouseenter', function(e, d) { d3.select(this).attr('r', d.group === 0 ? 12 : 8); })
    .on('mouseleave', function(e, d) { d3.select(this).attr('r', d.group === 0 ? 8 : 5); })
    .on('click', (e, d) => showDetail(d));

  node.append('text').text(d => d.name.length > 18 ? d.name.slice(0, 16) + '...' : d.name)
    .attr('dx', d => d.group === 0 ? 12 : 8).attr('dy', 4);

  sim.on('tick', () => {
    link.attr('d', graphLinkPath);
    linkHit.attr('d', graphLinkPath);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });

  document.getElementById('empty-state').style.display = 'none';
}

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function identityStatusMeta(d) {
  const status = d.match_status || 'unmatched';
  if (status === 'verified') return {label: '已确认', cls: 'verified'};
  if (status === 'review') return {label: '待人工确认', cls: 'review'};
  if (status === 'insufficient') return {label: '证据不足', cls: 'insufficient'};
  if (status === 'not-found') return {label: '未找到候选', cls: 'not-found'};
  if ((d.match_score || 0) > 30) return {label: '历史高分', cls: 'review'};
  if ((d.match_score || 0) > 0) return {label: '历史候选', cls: 'insufficient'};
  return {label: '未验证', cls: 'not-found'};
}

function showDetail(d) {
  state.selectedAuthor = d;
  var panel = document.getElementById('detail-panel');
  var content = document.getElementById('detail-content');
  var statusMeta = identityStatusMeta(d);
  var socialHTML = [
    { label: 'Google Scholar', url: d.google_scholar_url },
    { label: 'ResearchGate', url: d.researchgate_url },
    { label: 'Twitter / X', url: d.twitter_url },
    { label: 'LinkedIn', url: d.linkedin_url },
  ].map(function(s) {
    return '<a class="detail-link ' + (s.url ? '' : 'missing') + '" href="' + (s.url || '#') + '" target="_blank">' + s.label + (s.url ? '' : ' (未找到)') + '</a>';
  }).join('');
  var topicsHTML = (d.topics || []).map(function(t) { return '<span class="tag">' + t + '</span>'; }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  var affHTML = (d.affiliations || []).map(function(a) { return '<span class="tag">' + a + '</span>'; }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  var matchBadge = '<span class="identity-badge identity-' + statusMeta.cls + '">' +
    statusMeta.label + (d.match_score ? ' · ' + Number(d.match_score).toFixed(1) + '分' : '') +
    '</span>';
  var breakdown = d.match_breakdown || {};
  var breakdownRows = [
    ['姓名', breakdown.name],
    ['机构', breakdown.institution],
    ['研究领域', breakdown.topic],
    ['论文直接确认', breakdown.direct_work_authorship]
  ].filter(function(row) { return row[1] !== undefined && row[1] !== null; });
  var breakdownHTML = breakdownRows.length
    ? breakdownRows.map(function(row) {
        return '<div class="score-row"><span>' + row[0] + '</span><strong>' +
          escapeHTML(row[1]) + '</strong></div>';
      }).join('')
    : '<span class="detail-muted">暂无评分明细</span>';
  var candidatesHTML = (d.candidates || []).map(function(candidate) {
    var url = /^https?:\/\//.test(candidate.url || '') ? candidate.url : '#';
    var institutions = (candidate.affiliations || []).slice(0, 2).join('；') || '机构未知';
    return '<div class="candidate-card">' +
      '<div class="candidate-head"><a href="' + escapeHTML(url) +
      '" target="_blank" rel="noopener">' + escapeHTML(candidate.name || 'OpenAlex 候选') +
      '</a><strong>' + escapeHTML(candidate.score ?? 0) + '分</strong></div>' +
      '<div class="candidate-affiliation">' + escapeHTML(institutions) + '</div>' +
      '</div>';
  }).join('') || '<span class="detail-muted">没有可展示的 OpenAlex 候选</span>';
  var emailHTML = d.email ? '<div style="font-size:13px;color:#4ade80;margin-bottom:4px">' + d.email + '</div><div style="font-size:10px;color:var(--text-secondary)">来源: ' + (d.email_source || '未知') + '</div>' : '<span style="color:var(--text-secondary);font-size:11px">未找到邮箱</span>';
  var evidenceHTML = (d.sources || []).map(function(s) {
    var m = s.match(/(https?:\/\/[^\s]+)/);
    if (m) return '<div style="font-size:10px">' + s.substring(0, s.indexOf(m[0])) + '<a href="' + m[0] + '" target="_blank" style="color:var(--accent);word-break:break-all">' + m[0] + '</a></div>';
    return '<div style="font-size:10px;color:var(--text-secondary);word-break:break-all">' + s + '</div>';
  }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  content.innerHTML = '<div class="detail-name">' + escapeHTML(d.name) + matchBadge + '</div>' +
    '<div class="detail-section"><h4>身份匹配</h4>' + breakdownHTML + '</div>' +
    '<div class="detail-section"><h4>OpenAlex 候选</h4>' + candidatesHTML + '</div>' +
    '<div class="detail-section"><h4>邮箱</h4>' + emailHTML + '</div>' +
    '<div class="detail-section"><h4>机构</h4>' + affHTML + '</div>' +
    '<div class="detail-section"><h4>研究领域</h4>' + topicsHTML + '</div>' +
    '<div class="detail-section"><h4>社交媒体</h4>' + socialHTML + '</div>' +
    '<div class="detail-section"><h4>论据来源</h4>' + evidenceHTML + '</div>';
  panel.style.display = 'block';
}
document.getElementById('close-detail').addEventListener('click', () => {
  document.getElementById('detail-panel').style.display = 'none';
  state.selectedAuthor = null;
});

document.getElementById('zoom-in-btn').addEventListener('click', () => svg.transition().call(zoom.scaleBy, 1.3));
document.getElementById('zoom-out-btn').addEventListener('click', () => svg.transition().call(zoom.scaleBy, 0.7));
document.getElementById('zoom-fit-btn').addEventListener('click', () => svg.transition().call(zoom.transform, d3.zoomIdentity));
['show-coauthor', 'show-affiliation', 'show-topic'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    if (state.graph) renderGraph(state.graph);
  });
});

function runGraphSearch() {
  const input = document.getElementById('graph-search-input');
  state.graphSearch.type = document.getElementById('graph-search-type').value;
  state.graphSearch.query = input.value.trim();
  state.graphSearch.onlyMatches = document.getElementById('graph-search-only').checked;
  document.getElementById('graph-search-clear').style.display =
    state.graphSearch.query ? 'inline-flex' : 'none';
  if (state.graph) renderGraph(state.graph);
}

function clearGraphSearch(render = true) {
  state.graphSearch.query = '';
  state.graphSearch.onlyMatches = false;
  document.getElementById('graph-search-input').value = '';
  document.getElementById('graph-search-only').checked = false;
  document.getElementById('graph-search-clear').style.display = 'none';
  document.getElementById('graph-search-count').textContent = '';
  if (render && state.graph) renderGraph(state.graph);
}

document.getElementById('graph-search-btn').addEventListener('click', runGraphSearch);
document.getElementById('graph-search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') runGraphSearch();
});
document.getElementById('graph-search-clear').addEventListener('click', () => clearGraphSearch());
document.getElementById('graph-search-only').addEventListener('change', runGraphSearch);
document.getElementById('graph-search-type').addEventListener('change', e => {
  document.getElementById('graph-search-input').placeholder =
    e.target.value === 'institution' ? '输入大学或机构名称' : '输入人物姓名';
  if (state.graphSearch.query) runGraphSearch();
});

console.log('TalentMiner ready');

document.getElementById('download-csv-btn').addEventListener('click', async () => {
  if (!state.profiles || state.profiles.length === 0) {
    alert('没有可导出的数据，请先完成爬取');
    return;
  }
  try {
    const data = await api('POST', '/api/export-csv', { profiles: state.profiles });
    const blob = new Blob([data], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'authors_contacts.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setStatus('CSV 已下载', 'status-done');
  } catch (e) {
    alert('CSV 导出失败: ' + e.message);
  }
});

// History panel
async function loadHistory() {
  const list = document.getElementById('history-list');
  try {
    const projects = await api('GET', '/api/projects');
    if (!projects || projects.length === 0) {
      list.innerHTML = '<div class="history-empty">暂无历史记录<br><span style="font-size:10px">爬取完成后自动保存</span></div>';
      document.getElementById('download-csv-btn').style.display = 'none';
      return;
    }
    list.innerHTML = projects.map(p => `
      <div class="history-item" data-id="${p.id}">
        <div class="hi-title">${p.source_info || p.title || p.doi || p.id}</div>
        <div class="hi-meta">
          <span>${p.created_at || ''}</span>
          <span>${p.source_type || ''}</span>
        </div>
        <div class="history-actions">
          <button class="btn btn-sm load-btn" data-id="${p.id}">加载</button>
          <button class="btn btn-sm csv-btn" data-id="${p.id}">CSV</button>
        </div>
      </div>
    `).join('');
    // Wire load buttons
    list.querySelectorAll('.load-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = btn.dataset.id;
        await loadProject(id);
        // Highlight active
        list.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
        btn.closest('.history-item').classList.add('active');
      });
    });
    // Wire CSV buttons
    list.querySelectorAll('.csv-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await downloadProjectCSV(btn.dataset.id);
      });
    });
    // Click whole item to load
    list.querySelectorAll('.history-item').forEach(item => {
      item.addEventListener('click', () => {
        const id = item.dataset.id;
        loadProject(id);
        list.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="history-empty">加载失败: ${e.message}</div>`;
  }
}

async function loadProject(projectId) {
  setStatus('加载中...', 'status-running');
  try {
    const data = await api('GET', '/api/projects/' + projectId);
    state.profiles = data.authors;
    state.taskId = projectId;
    clearGraphSearch(false);
    // Show authors
    if (data.authors && data.authors.length > 0) {
      renderAuthorList(data.authors.map(a => a.name));
      document.getElementById('start-crawl-btn').style.display = 'none';
    }
    // Show graph if cached
    if (data.graph) {
      state.graph = data.graph;
      renderGraph(data.graph);
      renderStats(data.graph.stats || {});
      document.getElementById('empty-state').style.display = 'none';
      document.getElementById('network-stats').style.display = 'block';
      document.getElementById('download-csv-btn').style.display = 'block';
    } else if (state.profiles && state.profiles.length > 0) {
      await buildAndRenderGraph(projectId);
    }
    document.getElementById('progress-panel').style.display = 'none';
    setStatus('已加载: ' + projectId, 'status-done');
  } catch (e) {
    setStatus('加载失败', 'status-error');
    alert('加载项目失败: ' + e.message);
  }
}

async function downloadProjectCSV(projectId) {
  try {
    const res = await fetch(API + '/api/projects/' + projectId + '/csv');
    if (!res.ok) throw new Error('Download failed');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = projectId + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('CSV 下载失败: ' + e.message);
  }
}

document.getElementById('refresh-history-btn').addEventListener('click', loadHistory);

// Load history on startup and after crawl completes
loadHistory();

// Enhanced pollTask that also refreshes history
const _originalPollTask = pollTask;
pollTask = async function() {
  await _originalPollTask();
  if (state.profiles && state.profiles.length > 0) {
    try { loadHistory(); } catch(e) {}
    document.getElementById('download-csv-btn').style.display = 'block';
  }
};

// Enhanced renderStats
const _origRenderStats = renderStats;
renderStats = function(stats) {
  _origRenderStats(stats);
  document.getElementById('download-csv-btn').style.display = 'block';
};
