window.addEventListener("error", function(e) { console.error("GQI Talent Radar error:", e.message, e.filename, e.lineno); });
const API = '';
const state = {
  authors: [],
  profiles: [],
  graph: null,
  selectedAuthor: null,
  taskId: null,
  pollTimer: null,
  batchId: null,
  batchPollTimer: null,
  batchResultsLoaded: null,
  expansionJobId: null,
  expansionPollTimer: null,
  expansionScope: null,
  discoveredPapers: [],
  savedProjects: [],
  mergeSelectedProjectIds: new Set(),
  graphSearch: { type: 'person', query: '', onlyMatches: false }
};
const svg = d3.select('#graph-svg');
const container = document.getElementById('graph-container');
const linkTooltip = document.getElementById('graph-link-tooltip');
const RELATION_META = {
  'paper-author': {
    label: '论文署名',
    description: '大型合作论文通过论文中心节点连接作者，避免作者两两连线',
    color: '#e879f9',
    order: -1
  },
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
  },
  'lab-member': {
    label: '实验室成员',
    description: '实验室官网名单显示该人属于这个实验室',
    color: '#22d3ee',
    order: 3
  },
  'lab-pi': {
    label: '实验室 PI',
    description: '实验室官网明确标记的负责人',
    color: '#f472b6',
    order: 4
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
  const contentType = res.headers.get('content-type') || '';
  return contentType.includes('application/json') ? res.json() : res.text();
}

async function saveCsvBlob(blob, filename) {
  // pywebview does not consistently honor <a download> for blob URLs. Let the
  // desktop shell own the Save dialog; ordinary browsers keep native downloads.
  if (window.pywebview && window.pywebview.api && window.pywebview.api.save_csv) {
    const result = await window.pywebview.api.save_csv(await blob.text(), filename);
    if (!result || result.status === 'error') {
      throw new Error((result && result.message) || '桌面端保存失败');
    }
    return result.status === 'saved';
  }

  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return true;
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

function localISODate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

const paperDateTo = document.getElementById('paper-date-to');
const paperDateFrom = document.getElementById('paper-date-from');
if (paperDateTo && paperDateFrom) {
  const today = new Date();
  const fiveYearsAgo = new Date(today.getFullYear() - 5, today.getMonth(), today.getDate());
  paperDateTo.value = localISODate(today);
  paperDateFrom.value = localISODate(fiveYearsAgo);
}

function selectedDiscoveredPapers() {
  return Array.from(document.querySelectorAll('.paper-select:checked'))
    .map(input => state.discoveredPapers[Number(input.dataset.index)])
    .filter(Boolean);
}

function refreshPaperStartButton() {
  const selected = selectedDiscoveredPapers().length;
  const button = document.getElementById('paper-start-btn');
  if (button) {
    button.disabled = selected === 0;
    button.textContent = selected
      ? `处理所选 ${selected} 篇论文并搜集联系方式`
      : '处理所选论文并搜集联系方式';
  }
}

function renderPaperPreview(result) {
  state.discoveredPapers = result.papers || [];
  const sourceLabel = result.metadata_source === 'crossref' ? 'Crossref' : 'OpenAlex';
  document.getElementById('paper-preview-panel').style.display = 'block';
  document.getElementById('paper-preview-summary').textContent =
    `${sourceLabel} · 严格筛选后预览 ${result.returned || 0} 篇` +
    `（数据源初始约命中 ${result.estimated_total || 0} 篇）` +
    (result.relevance_filtered ? ` · 已过滤低相关论文 ${result.relevance_filtered} 篇` : '') +
    (result.duplicates_removed ? ` · 已自动去重 ${result.duplicates_removed} 条` : '') +
    (result.unavailable_removed ? ` · 已过滤失效论文 ${result.unavailable_removed} 条` : '') +
    (result.notice ? ` · ${result.notice}` : '');
  document.getElementById('paper-preview-list').innerHTML = state.discoveredPapers.map((paper, index) => {
    const authors = (paper.authors || []).slice(0, 4).join('、');
    const suffix = (paper.authors || []).length > 4 ? ` 等${paper.author_count}人` : '';
    return `<label class="paper-preview-item">` +
      `<input type="checkbox" class="paper-select" data-index="${index}" checked>` +
      `<span class="paper-preview-body"><strong>${escapeHTML(paper.title || '未命名论文')}</strong>` +
      `<span>${escapeHTML(paper.publication_date || '日期未知')} · ${escapeHTML(paper.venue || paper.type || '')}` +
      ` · ${paper.pdf_url ? '有开放PDF' : '元数据处理'}` +
      (paper.relevance_score ? ` · 相关性 ${paper.relevance_score}` : '') +
      `</span>` +
      (paper.relevance_reason ? `<span>${escapeHTML(paper.relevance_reason)}</span>` : '') +
      `<span>${escapeHTML(authors + suffix)}</span></span></label>`;
  }).join('') || '<div class="history-empty">没有找到符合条件的论文</div>';
  document.querySelectorAll('.paper-select').forEach(input => input.addEventListener('change', refreshPaperStartButton));
  refreshPaperStartButton();
}

document.getElementById('paper-preview-btn').addEventListener('click', async () => {
  const query = document.getElementById('paper-query').value.trim();
  if (!query) return alert('请输入研究主题或关键词');
  const payload = {
    query,
    date_from: paperDateFrom.value,
    date_to: paperDateTo.value,
    limit: Number(document.getElementById('paper-limit').value || 100),
    open_access_only: document.getElementById('paper-oa-only').checked
  };
  setStatus('正在检索历史论文...', 'status-running');
  const button = document.getElementById('paper-preview-btn');
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = '正在搜索，请稍候...';
  document.getElementById('paper-preview-panel').style.display = 'none';
  try {
    renderPaperPreview(await api('POST', '/api/paper-search/preview', payload));
    setStatus('论文检索完成', 'status-done');
  } catch (e) {
    setStatus('论文检索失败', 'status-error');
    alert('论文检索失败: ' + e.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
});

document.getElementById('paper-toggle-all').addEventListener('click', () => {
  const inputs = Array.from(document.querySelectorAll('.paper-select'));
  const shouldCheck = inputs.some(input => !input.checked);
  inputs.forEach(input => { input.checked = shouldCheck; });
  refreshPaperStartButton();
});

document.getElementById('paper-start-btn').addEventListener('click', async () => {
  const papers = selectedDiscoveredPapers();
  if (!papers.length) return;
  const payload = {
    query: document.getElementById('paper-query').value.trim(),
    date_from: paperDateFrom.value,
    date_to: paperDateTo.value,
    author_scope: document.getElementById('paper-author-scope').value,
    papers
  };
  const button = document.getElementById('paper-start-btn');
  button.disabled = true;
  setStatus('正在创建历史回溯任务...', 'status-running');
  try {
    const job = await api('POST', '/api/paper-search/batches', payload);
    state.batchId = job.id;
    state.batchResultsLoaded = null;
    localStorage.setItem('talentminer.lastBatchId', job.id);
    renderBatchJob(job);
    startBatchPolling();
  } catch (e) {
    setStatus('历史回溯任务创建失败', 'status-error');
    alert('任务创建失败: ' + e.message);
    refreshPaperStartButton();
  }
});

// PDF upload
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag-over');
  handleSelectedPDFs(Array.from(e.dataTransfer.files || []));
});
fileInput.addEventListener('change', () => {
  handleSelectedPDFs(Array.from(fileInput.files || []));
  fileInput.value = '';
});

function handleSelectedPDFs(files) {
  const pdfs = files.filter(file => file.name.toLowerCase().endsWith('.pdf'));
  if (!pdfs.length) {
    alert('请选择 PDF 文件');
    return;
  }
  if (pdfs.length !== files.length) {
    alert('已忽略非 PDF 文件');
  }
  if (pdfs.length === 1) handlePDFUpload(pdfs[0]);
  else handleBatchUpload(pdfs);
}

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

const BATCH_STATUS_META = {
  queued: { label: '等待中', icon: '○' },
  running: { label: '处理中', icon: '◌' },
  completed: { label: '已完成', icon: '✓' },
  skipped: { label: '已跳过重复人物', icon: '↷' },
  partial: { label: '部分完成', icon: '!' },
  failed: { label: '失败', icon: '×' }
};

async function handleBatchUpload(files) {
  if (files.length > 20) {
    alert('一次最多提交 20 篇 PDF，请分批上传');
    return;
  }
  if (state.batchPollTimer) clearInterval(state.batchPollTimer);
  state.batchResultsLoaded = null;
  document.getElementById('batch-panel').style.display = 'block';
  document.getElementById('batch-title').textContent = `正在上传 ${files.length} 篇论文`;
  document.getElementById('batch-progress-text').textContent = '正在接收文件...';
  document.getElementById('batch-items').innerHTML = files.map(file =>
    `<div class="batch-item"><span class="batch-item-icon">○</span>` +
    `<span class="batch-item-name">${escapeHTML(file.name)}</span>` +
    `<span class="batch-item-meta">上传中</span></div>`
  ).join('');
  document.getElementById('batch-download-btn').style.display = 'none';
  document.getElementById('author-list-container').style.display = 'none';
  document.getElementById('crawl-action-panel').style.display = 'none';
  setStatus('上传批量论文...', 'status-running');
  const form = new FormData();
  files.forEach(file => form.append('files', file));
  try {
    const job = await api('POST', '/api/batches', form);
    state.batchId = job.id;
    localStorage.setItem('talentminer.lastBatchId', job.id);
    renderBatchJob(job);
    startBatchPolling();
  } catch (e) {
    setStatus('批量任务创建失败', 'status-error');
    document.getElementById('batch-progress-text').textContent = e.message;
    alert('批量任务创建失败: ' + e.message);
  }
}

function startBatchPolling() {
  if (state.batchPollTimer) clearInterval(state.batchPollTimer);
  pollBatch();
  state.batchPollTimer = setInterval(pollBatch, 2000);
}

function renderBatchJob(job) {
  const status = BATCH_STATUS_META[job.status] || BATCH_STATUS_META.queued;
  const total = Number(job.total || 0);
  const completed = Number(job.completed || 0);
  const pct = total ? Math.round(completed / total * 100) : 0;
  const statusEl = document.getElementById('batch-status');
  document.getElementById('batch-panel').style.display = 'block';
  document.getElementById('batch-title').textContent = `批量任务 · ${total} 篇`;
  statusEl.textContent = status.label;
  statusEl.className = `batch-status batch-${job.status}`;
  document.getElementById('batch-progress-fill').style.width = pct + '%';
  let progressText = `已处理 ${completed}/${total} · 成功 ${job.succeeded || 0} · 失败 ${job.failed || 0}`;
  if (job.current_filename) progressText += ` · 当前：${job.current_filename}`;
  if (job.error) progressText += ` · ${job.error}`;
  document.getElementById('batch-progress-text').textContent = progressText;
  document.getElementById('batch-items').innerHTML = (job.items || []).map(item => {
    const itemStatus = BATCH_STATUS_META[item.status] || BATCH_STATUS_META.queued;
    const resultMeta = item.status === 'completed'
      ? `${item.profile_count || 0} 位作者`
      : itemStatus.label;
    return `<div class="batch-item">` +
      `<span class="batch-item-icon">${itemStatus.icon}</span>` +
      `<span class="batch-item-name" title="${escapeHTML(item.original_filename)}">${escapeHTML(item.original_filename)}</span>` +
      `<span class="batch-item-meta">${escapeHTML(resultMeta)}</span>` +
      (item.error && ['failed', 'skipped'].includes(item.status)
        ? `<span class="batch-item-error">${escapeHTML(item.error)}</span>` : '') +
      `</div>`;
  }).join('');
}

async function pollBatch() {
  if (!state.batchId) return;
  try {
    const job = await api('GET', '/api/batches/' + encodeURIComponent(state.batchId));
    renderBatchJob(job);
    if (['completed', 'partial', 'failed'].includes(job.status)) {
      if (state.batchPollTimer) clearInterval(state.batchPollTimer);
      state.batchPollTimer = null;
      await loadBatchContacts(job);
    } else {
      setStatus(`批量处理中 ${job.completed || 0}/${job.total || 0}`, 'status-running');
    }
  } catch (e) {
    setStatus('批量进度读取失败', 'status-error');
    document.getElementById('batch-progress-text').textContent = e.message;
  }
}

async function loadBatchContacts(job) {
  if (state.batchResultsLoaded === job.id) return;
  const data = await api('GET', '/api/batches/' + encodeURIComponent(job.id) + '/contacts');
  state.batchResultsLoaded = job.id;
  state.profiles = data.contacts || [];
  state.taskId = null;
  document.getElementById('batch-download-btn').style.display = state.profiles.length ? 'block' : 'none';
  document.getElementById('batch-progress-text').textContent =
    `已处理 ${job.completed}/${job.total} · 汇总 ${data.contact_count} 位联系人 · ` +
    `${data.contacts_with_email} 个邮箱 · ${data.contacts_with_phone} 个电话`;
  if (state.profiles.length) {
    clearGraphSearch(false);
    showBatchContactsReady(data.contact_count);
    document.getElementById('download-csv-btn').style.display = 'none';
  }
  if (job.status === 'completed') setStatus('批量任务完成', 'status-done');
  else if (job.status === 'partial') setStatus('批量任务部分完成', 'status-done');
  else setStatus('批量任务失败', 'status-error');
  loadHistory();
}

function showBatchContactsReady(contactCount) {
  state.graph = null;
  if (g) g.selectAll('*').remove();
  hideLinkTooltip();
  document.getElementById('detail-panel').style.display = 'none';
  document.getElementById('network-stats').style.display = 'none';
  document.getElementById('empty-state').style.display = 'flex';
  document.querySelector('#empty-state p').textContent =
    `已汇总 ${contactCount || 0} 位联系人。请点击左侧“合并联系方式”创建历史汇总任务，再从历史记录加载关系图。`;
}

async function restoreLastBatch() {
  const batchId = localStorage.getItem('talentminer.lastBatchId');
  if (!batchId) return;
  state.batchId = batchId;
  try {
    const job = await api('GET', '/api/batches/' + encodeURIComponent(batchId));
    renderBatchJob(job);
    if (['completed', 'partial', 'failed'].includes(job.status)) {
      await loadBatchContacts(job);
    } else {
      startBatchPolling();
    }
  } catch (e) {
    localStorage.removeItem('talentminer.lastBatchId');
  }
}

document.getElementById('batch-download-btn').addEventListener('click', async () => {
  if (!state.batchId) return;
  try {
    const res = await fetch(API + '/api/batches/' + encodeURIComponent(state.batchId) + '/csv');
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.detail || '下载失败');
    }
    await saveCsvBlob(await res.blob(), state.batchId + '-contacts.csv');
  } catch (e) {
    alert('批量 CSV 下载失败: ' + e.message);
  }
});

function currentExpansionScope() {
  if (state.taskId) return {type: 'project', id: state.taskId};
  if (state.batchId) return {type: 'batch', id: state.batchId};
  return null;
}

function expansionCandidateFromNode(node) {
  const parentNames = node.parent_names || [];
  const parentIds = node.parent_openalex_ids || [];
  let parentIndex = parentIds.findIndex(Boolean);
  if (parentIndex < 0) return null;
  return {
    name: node.name,
    openalex_id: node.openalex_id || '',
    parent_name: parentNames[parentIndex] || parentNames[0] || '',
    parent_openalex_id: parentIds[parentIndex],
    field_relevant: node.field_relevant !== false,
    value_score: Number(node.expansion_value_score || 0),
    shared_works_count: Number(node.shared_works_count || 0),
    recent_shared_works_count: Number(node.recent_shared_works_count || 0),
    latest_shared_year: Number(node.latest_shared_year || 0)
  };
}

function labExpansionCandidateFromNode(node, graph = state.graph) {
  if (!node || node.group !== 2) return null;
  const parentNames = node.parent_names || [];
  const parentIds = node.parent_openalex_ids || [];
  let parentName = node.lab_pi || parentNames[0] || '';
  let parentId = parentIds.find(Boolean) || '';
  const parentNode = (graph?.nodes || []).find(item =>
    item.name === parentName || (parentNames || []).includes(item.name));
  if (!parentId && parentNode) parentId = parentNode.openalex_id || '';
  if (!parentName && parentNode) parentName = parentNode.name || '';
  const affiliations = (node.affiliations || []).length
    ? node.affiliations
    : (parentNode?.affiliations || []);
  return {
    candidate_type: 'lab-member',
    name: node.name,
    openalex_id: node.openalex_id || '',
    parent_name: parentName,
    parent_openalex_id: parentId,
    lab_name: node.lab_name || '',
    lab_url: node.lab_url || '',
    directory_url: node.directory_url || '',
    lab_pi: node.lab_pi || '',
    profile_url: node.website_url || '',
    email: node.email || '',
    email_source: node.email_source || '',
    email_confidence: Number(node.email_confidence || 0),
    phone: node.phone || '',
    phone_source: node.phone_source || '',
    phone_confidence: Number(node.phone_confidence || 0),
    career_stage: node.career_stage || 'graduate-student',
    degree_type: node.degree_type || '',
    evidence: 'Official laboratory roster',
    affiliations,
    student_score: Number(node.student_score || 82),
    student_status: node.student_status || 'confirmed-student',
    student_evidence: node.student_evidence || [],
    value_score: (node.website_url ? 25 : 0) + (parentId ? 20 : 0) +
      ((node.email || node.phone) ? 30 : 0)
  };
}

function expandableLabMembers(graph = state.graph) {
  const seen = new Set();
  return (graph?.nodes || [])
    .filter(node => node.group === 2 &&
      (!node.contact_search_status || node.contact_search_status === 'not-started'))
    .map(node => labExpansionCandidateFromNode(node, graph))
    .filter(Boolean)
    .filter(candidate => {
      const key = candidate.openalex_id || `${candidate.lab_url}:${candidate.name.toLowerCase()}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((a, b) => b.value_score - a.value_score || a.name.localeCompare(b.name));
}

function expandableCollaborators(graph = state.graph) {
  const candidates = (graph?.nodes || [])
    .filter(node => node.group === 1 && node.field_relevant !== false)
    .map(expansionCandidateFromNode)
    .filter(Boolean);
  const seen = new Set();
  return candidates.filter(candidate => {
    const key = candidate.openalex_id || `${candidate.parent_openalex_id}:${candidate.name.toLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((a, b) =>
    b.value_score - a.value_score ||
    b.recent_shared_works_count - a.recent_shared_works_count ||
    b.shared_works_count - a.shared_works_count ||
    b.latest_shared_year - a.latest_shared_year
  );
}

function updateExpansionActions(graph = state.graph) {
  const scope = currentExpansionScope();
  const candidates = expandableCollaborators(graph);
  const labCandidates = expandableLabMembers(graph);
  const panel = document.getElementById('expansion-panel');
  if (!scope || (!candidates.length && !labCandidates.length)) {
    if (state.expansionPollTimer) {
      panel.style.display = 'block';
      return;
    }
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  document.getElementById('expansion-summary').textContent =
    `发现 ${candidates.length} 位紫色合作者、${labCandidates.length} 位待搜索实验室成员`;
  document.getElementById('expand-top-btn').disabled = Boolean(state.expansionPollTimer);
  document.getElementById('expand-top-btn').style.display = candidates.length ? 'block' : 'none';
  document.getElementById('expand-lab-btn').disabled = Boolean(state.expansionPollTimer);
  document.getElementById('expand-lab-btn').style.display = labCandidates.length ? 'block' : 'none';
}

async function startCollaboratorExpansion(candidates) {
  const scope = currentExpansionScope();
  if (!scope) {
    alert('请先加载一个项目或批量结果');
    return;
  }
  const input = (candidates || []).filter(Boolean);
  const isLabJob = input[0]?.candidate_type === 'lab-member';
  const selected = input.slice(0, isLabJob ? 30 : 5);
  if (!selected.length) {
    alert('该合作者缺少可验证的目标作者关系');
    return;
  }
  if (state.expansionPollTimer) {
    alert('已有合作者扩展任务正在运行');
    return;
  }
  setStatus('扩展合作者网络...', 'status-running');
  document.getElementById('expansion-panel').style.display = 'block';
  document.getElementById('expansion-progress').style.display = 'block';
  document.getElementById('expand-top-btn').disabled = true;
  document.getElementById('expand-lab-btn').disabled = true;
  try {
    const job = await api('POST', '/api/expansions', {
      scope_type: scope.type,
      scope_id: scope.id,
      candidates: selected
    });
    state.expansionJobId = job.id;
    state.expansionScope = {type: job.scope_type, id: job.scope_id};
    localStorage.setItem('talentminer.lastExpansionJobId', job.id);
    renderExpansionJob(job);
    startExpansionPolling();
  } catch (e) {
    document.getElementById('expand-top-btn').disabled = false;
    document.getElementById('expand-lab-btn').disabled = false;
    setStatus('合作者扩展失败', 'status-error');
    alert('无法创建合作者扩展任务: ' + e.message);
  }
}

function renderExpansionJob(job) {
  const total = Number(job.total || 0);
  const completed = Number(job.completed || 0);
  const pct = total ? Math.round(completed / total * 100) : 0;
  document.getElementById('expansion-panel').style.display = 'block';
  document.getElementById('expansion-progress').style.display = 'block';
  document.getElementById('expansion-progress-fill').style.width = pct + '%';
  let text = `已处理 ${completed}/${total} · 成功 ${job.succeeded || 0} · 跳过 ${job.skipped || 0} · 失败 ${job.failed || 0}`;
  if (job.current_name) text += ` · 当前：${job.current_name}`;
  if (Number(job.recovery_count || 0) > 0) {
    text += ` · 服务中断后已自动恢复 ${job.recovery_count} 次`;
  }
  document.getElementById('expansion-progress-text').textContent = text;
  document.getElementById('expansion-items').innerHTML = (job.items || []).map(item => {
    const meta = BATCH_STATUS_META[item.status] || BATCH_STATUS_META.queued;
    return `<div class="batch-item"><span class="batch-item-icon">${meta.icon}</span>` +
      `<span class="batch-item-name">${escapeHTML(item.name)}</span>` +
      `<span class="batch-item-meta">${escapeHTML(item.stage || meta.label)}</span>` +
      (item.error && item.status === 'failed'
        ? `<span class="batch-item-error">${escapeHTML(item.error)}</span>` : '') +
      `</div>`;
  }).join('');
}

function startExpansionPolling() {
  if (state.expansionPollTimer) clearInterval(state.expansionPollTimer);
  pollExpansion();
  state.expansionPollTimer = setInterval(pollExpansion, 2000);
}

async function pollExpansion() {
  if (!state.expansionJobId) return;
  try {
    const job = await api('GET', '/api/expansions/' + encodeURIComponent(state.expansionJobId));
    state.expansionScope = {type: job.scope_type, id: job.scope_id};
    renderExpansionJob(job);
    if (['completed', 'partial', 'failed'].includes(job.status)) {
      if (state.expansionPollTimer) clearInterval(state.expansionPollTimer);
      state.expansionPollTimer = null;
      document.getElementById('expand-top-btn').disabled = false;
      document.getElementById('expand-lab-btn').disabled = false;
      await refreshAfterExpansion(job);
      setStatus(
        job.status === 'failed' ? '合作者扩展失败' : '合作者扩展完成',
        job.status === 'failed' ? 'status-error' : 'status-done'
      );
    }
  } catch (e) {
    document.getElementById('expansion-progress-text').textContent = e.message;
  }
}

async function refreshAfterExpansion(job) {
  if (job.scope_type === 'batch') {
    const data = await api('GET', '/api/batches/' + encodeURIComponent(job.scope_id) + '/contacts');
    state.profiles = data.contacts || [];
    state.taskId = null;
    showBatchContactsReady(data.contact_count);
  } else {
    await loadProject(job.scope_id);
  }
}

async function restoreLastExpansion() {
  const jobId = localStorage.getItem('talentminer.lastExpansionJobId');
  if (!jobId) return;
  state.expansionJobId = jobId;
  try {
    const job = await api('GET', '/api/expansions/' + encodeURIComponent(jobId));
    state.expansionScope = {type: job.scope_type, id: job.scope_id};
    renderExpansionJob(job);
    if (!['completed', 'partial', 'failed'].includes(job.status)) {
      startExpansionPolling();
    }
  } catch (e) {
    localStorage.removeItem('talentminer.lastExpansionJobId');
  }
}

document.getElementById('expand-top-btn').addEventListener('click', () => {
  startCollaboratorExpansion(expandableCollaborators().slice(0, 5));
});
document.getElementById('expand-lab-btn').addEventListener('click', () => {
  startCollaboratorExpansion(expandableLabMembers().slice(0, 30));
});

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
      if (data.lab_expansion_job_id) {
        state.expansionJobId = data.lab_expansion_job_id;
        state.expansionScope = {type: 'project', id: state.taskId};
        localStorage.setItem('talentminer.lastExpansionJobId', data.lab_expansion_job_id);
        logCrawl('已自动排队核对实验室成员身份并搜索联系方式');
        startExpansionPolling();
      }
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
      project_id: projectId,
      paper_scope: projectId ? 'single' : 'aggregate'
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
    <div class="stat-row"><span>临近毕业候选</span><span class="val">${stats.graduation_candidate_count || 0}</span></div>
    <div class="stat-row"><span>中国关联证据</span><span class="val">${stats.china_linked_count || 0}</span></div>
    <div class="stat-row"><span>官网发现学生</span><span class="val">${stats.official_roster_candidate_count || 0}</span></div>
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
    if (!document.getElementById('show-coauthor').checked && ['co-author', 'paper-author'].includes(e.relation)) return false;
    if (!document.getElementById('show-affiliation').checked && e.relation === 'same-affiliation') return false;
    if (!document.getElementById('show-topic').checked && e.relation === 'shared-topic') return false;
    if (!document.getElementById('show-lab').checked && ['lab-member', 'lab-pi'].includes(e.relation)) return false;
    return true;
  });
  const linkedNodes = new Set();
  linkData.forEach(l => { linkedNodes.add(l.source); linkedNodes.add(l.target); });
  let nodeData = nodeDataAll.filter(n => linkedNodes.has(n.id) || n.group === 0);

  const graduatingOnly = document.getElementById('show-graduating-only')?.checked;
  const chinaLinkedOnly = document.getElementById('show-china-linked-only')?.checked;
  const studentsOnly = document.getElementById('show-students-only')?.checked;
  if (graduatingOnly || chinaLinkedOnly || studentsOnly) {
    nodeData = nodeData.filter(n => {
      const graduationMatch = ['confirmed-upcoming', 'likely-upcoming'].includes(n.graduation_status);
      const chinaMatch = Number(n.china_link_score || 0) > 0;
      const studentMatch = ['confirmed-student', 'likely-student', 'possible-student'].includes(n.student_status);
      return (!graduatingOnly || graduationMatch) && (!chinaLinkedOnly || chinaMatch) &&
        (!studentsOnly || studentMatch);
    });
    const candidateIds = new Set(nodeData.map(n => n.id));
    linkData = linkData.filter(e => candidateIds.has(e.source) && candidateIds.has(e.target));
  }

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

  // A stale cached graph may still contain tens of thousands of links. Never
  // hand that entire array to SVG and the force simulation: prioritise useful
  // collaboration/lab relationships and keep the page interactive.
  const renderableLinkCount = linkData.length;
  const maxRenderedLinks = 2500;
  if (linkData.length > maxRenderedLinks) {
    const relationPriority = {
      'paper-author': 0, 'lab-pi': 1, 'lab-member': 2,
      'co-author': 3, 'same-affiliation': 4, 'shared-topic': 5
    };
    linkData.sort((a, b) =>
      (relationPriority[a.relation] ?? 9) - (relationPriority[b.relation] ?? 9) ||
      Number(b.weight || 0) - Number(a.weight || 0));
    linkData = linkData.slice(0, maxRenderedLinks);
  }
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
  if (countEl) {
    const relationNotice = renderableLinkCount > maxRenderedLinks
      ? ` · 已显示 ${maxRenderedLinks}/${renderableLinkCount} 条关系`
      : '';
    countEl.textContent = (hasSearch ? `匹配 ${matchedIds.size} 人` : '') + relationNotice;
  }

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
    .attr('r', d => d.group === 0 ? 8 : [3, 5].includes(d.group) ? 11 : d.group === 2 ? 7 : 5)
    .attr('fill', d => {
      if (hasSearch && matchedIds.has(d.id)) return '#facc15';
      if (['confirmed-upcoming', 'likely-upcoming'].includes(d.graduation_status)) return '#22d3ee';
      if (d.graduation_status === 'student') return '#14b8a6';
      return d.group === 0 ? '#6c8cff' : d.group === 5 ? '#e879f9' : d.group === 3 ? '#f472b6' : d.group === 2 ? '#22d3ee' : '#a78bfa';
    })
    .attr('stroke', d => {
      if (hasSearch && matchedIds.has(d.id)) return '#fff3a3';
      if (d.contact_search_status === 'contact-found') return '#22c55e';
      if (d.contact_search_status === 'completed-no-contact') return '#94a3b8';
      if (d.contact_search_status === 'identity-review') return '#ef4444';
      if (Number(d.china_link_score || 0) > 0) return '#facc15';
      return d.group === 0 ? '#8ba3ff' : '#c4b5fd';
    })
    .attr('stroke-width', d => d.contact_search_status !== 'not-started' ? 3 : 1.5)
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
  updateExpansionActions(data);
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

function careerStageLabel(stage) {
  return ({
    'doctoral-student': '博士生', 'masters-student': '硕士生',
    'graduate-student': '研究生（学位未确认）',
    'recent-graduate': '近期毕业/前成员',
    'postdoc': '博士后', 'faculty': '教师/教授', 'staff': '科研人员',
    'unknown': '身份未知'
  })[stage || 'unknown'] || stage;
}

function graduationStatusLabel(status) {
  return ({
    'confirmed-upcoming': '明确临近毕业',
    'likely-upcoming': '大概率临近毕业',
    'student': '已确认学生，毕业时间不足',
    'not-student': '当前不是在读学生',
    'graduated': '已毕业',
    'insufficient': '信息不足'
  })[status || 'insufficient'] || status;
}

function studentStatusLabel(status) {
  return ({
    'confirmed-student': '已确认学生', 'likely-student': '大概率是学生',
    'possible-student': '可能是学生', 'unknown': '信息不足',
    'likely-non-student': '大概率非学生',
    'confirmed-non-student': '已确认非学生'
  })[status || 'unknown'] || status;
}

function evidenceListHTML(items, emptyText) {
  return (items || []).map(function(item) {
    var url = /^https?:\/\//.test(item.source || '') ? item.source : '';
    return '<div class="candidate-card"><div class="candidate-head"><span>' +
      escapeHTML(item.value || item.type || '公开证据') + '</span><strong>' +
      escapeHTML(item.confidence || 0) + '</strong></div>' +
      '<div class="candidate-affiliation">' + escapeHTML(item.type || '') +
      (url ? ' · <a href="' + escapeHTML(url) + '" target="_blank" rel="noopener">来源</a>' : '') +
      '</div></div>';
  }).join('') || '<span class="detail-muted">' + escapeHTML(emptyText) + '</span>';
}

function showDetail(d) {
  state.selectedAuthor = d;
  var panel = document.getElementById('detail-panel');
  var content = document.getElementById('detail-content');
  if (d.group === 1) {
    var expansionCandidate = expansionCandidateFromNode(d);
    var parentText = (d.parent_names || []).join('、') || '目标作者';
    var topicsText = (d.shared_topics || []).join('；') || '尚未取得合作论文主题';
    content.innerHTML = '<div class="detail-name">' + escapeHTML(d.name) +
      '<span class="identity-badge identity-review">紫色合作者</span></div>' +
      '<div class="external-node-note">当前仅确认此人与目标作者存在共同署名关系。深度搜集前会回到共同论文解析 OpenAlex ID，不会仅凭同名匹配。</div>' +
      '<div class="detail-section"><h4>合作价值</h4>' +
      '<div class="score-row"><span>关联目标作者</span><strong>' + escapeHTML(parentText) + '</strong></div>' +
      '<div class="score-row"><span>合作论文样本</span><strong>' + escapeHTML(d.shared_works_count || 1) + '</strong></div>' +
      '<div class="score-row"><span>近年合作</span><strong>' + escapeHTML(d.recent_shared_works_count || 0) + '</strong></div>' +
      '<div class="score-row"><span>最近合作年份</span><strong>' + escapeHTML(d.latest_shared_year || '未知') + '</strong></div>' +
      '<div class="score-row"><span>扩展价值分</span><strong>' + escapeHTML(d.expansion_value_score || 0) + '</strong></div></div>' +
      '<div class="detail-section"><h4>合作主题</h4><span class="detail-muted">' + escapeHTML(topicsText) + '</span></div>' +
      '<button id="expand-this-collaborator" class="btn btn-primary btn-full" ' +
      (expansionCandidate ? '' : 'disabled') + '>深度搜集此合作者</button>';
    panel.style.display = 'block';
    var expandButton = document.getElementById('expand-this-collaborator');
    if (expandButton && expansionCandidate) {
      expandButton.addEventListener('click', () => startCollaboratorExpansion([expansionCandidate]));
    }
    return;
  }
  if (d.group === 2) {
    var memberSource = d.directory_url || d.lab_url || d.website_url || '';
    var labCandidate = labExpansionCandidateFromNode(d);
    content.innerHTML = '<div class="detail-name">' + escapeHTML(d.name) +
      '<span class="identity-badge identity-verified">实验室成员</span></div>' +
      '<div class="external-node-note">此人来自已核验的实验室官网名单；联系方式只从本人成员页采用。</div>' +
      '<div class="detail-section"><h4>学生身份</h4>' +
      '<div class="score-row"><span>判断</span><strong>' + escapeHTML(studentStatusLabel(d.student_status)) + '</strong></div>' +
      '<div class="score-row"><span>评分</span><strong>' + escapeHTML(Number(d.student_score || 0).toFixed(0)) + '</strong></div>' +
      '<div class="score-row"><span>官网角色</span><strong>' + escapeHTML(careerStageLabel(d.career_stage)) + '</strong></div></div>' +
      '<div class="detail-section"><h4>联系方式</h4><div class="contact-value">' + escapeHTML(d.email || '未找到公开邮箱') + '</div>' +
      (d.phone ? '<div class="contact-value">' + escapeHTML(d.phone) + '</div>' : '') + '</div>' +
      '<div class="detail-section"><h4>实验室</h4><div>' + escapeHTML(d.lab_name || '实验室名称未识别') + '</div>' +
      (memberSource ? '<a href="' + escapeHTML(memberSource) + '" target="_blank" rel="noopener">查看官网证据</a>' : '') +
      (d.website_url ? ' · <a href="' + escapeHTML(d.website_url) + '" target="_blank" rel="noopener">成员个人页</a>' : '') + '</div>' +
      '<div class="detail-section"><h4>深度搜索状态</h4>' +
      '<div class="score-row"><span>身份</span><strong>' + escapeHTML(d.identity_status || '待核对') + '</strong></div>' +
      '<div class="score-row"><span>联系方式</span><strong>' + escapeHTML(d.contact_search_status || '未开始') + '</strong></div>' +
      (d.search_failure_reason ? '<div class="external-node-note">' + escapeHTML(d.search_failure_reason) + '</div>' : '') + '</div>' +
      '<button id="expand-this-lab-member" class="btn btn-primary btn-full" ' +
      (labCandidate ? '' : 'disabled') + '>核对身份并深度搜集</button>';
    panel.style.display = 'block';
    var labExpandButton = document.getElementById('expand-this-lab-member');
    if (labExpandButton && labCandidate) {
      labExpandButton.addEventListener('click', () => startCollaboratorExpansion([labCandidate]));
    }
    return;
  }
  if (d.group === 3) {
    content.innerHTML = '<div class="detail-name">' + escapeHTML(d.name) +
      '<span class="identity-badge identity-review">实验室</span></div>' +
      '<div class="external-node-note">这是独立的实验室实体，连线表示 PI 或成员隶属，不表示该网页由某个成员“引出”。</div>' +
      '<div class="detail-section"><h4>实验室信息</h4>' +
      '<div class="score-row"><span>PI</span><strong>' + escapeHTML(d.lab_pi || '未确认') + '</strong></div>' +
      (d.directory_url ? '<a href="' + escapeHTML(d.directory_url) + '" target="_blank" rel="noopener">成员名单</a>' : '') +
      (d.lab_url ? ' · <a href="' + escapeHTML(d.lab_url) + '" target="_blank" rel="noopener">实验室官网</a>' : '') + '</div>';
    panel.style.display = 'block';
    return;
  }
  var statusMeta = identityStatusMeta(d);
  var socialHTML = [
    { label: '机构/个人主页', url: d.website_url },
    { label: 'ORCID', url: d.orcid },
    { label: 'Google Scholar', url: d.google_scholar_url },
    { label: 'ResearchGate', url: d.researchgate_url },
    { label: 'Twitter / X', url: d.twitter_url },
    { label: 'LinkedIn', url: d.linkedin_url },
  ].map(function(s) {
    return '<a class="detail-link ' + (s.url ? '' : 'missing') + '" href="' + (s.url || '#') + '" target="_blank">' + s.label + (s.url ? '' : ' (未找到)') + '</a>';
  }).join('');
  var topicsHTML = (d.topics || []).map(function(t) { return '<span class="tag">' + t + '</span>'; }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  var affHTML = (d.affiliations || []).map(function(a) { return '<span class="tag">' + a + '</span>'; }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  var aliasesHTML = (d.name_aliases || []).filter(function(alias) {
    return alias && alias.toLowerCase() !== String(d.name || '').toLowerCase();
  }).map(function(alias) { return '<span class="tag">' + escapeHTML(alias) + '</span>'; }).join('') ||
    '<span class="detail-muted">未获得其他中文名或拼音变体</span>';
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
  var emailHTML = d.email
    ? '<div style="font-size:13px;color:#4ade80;margin-bottom:4px">' + escapeHTML(d.email) + '</div>' +
      '<div style="font-size:10px;color:var(--text-secondary)">可信度: ' +
      escapeHTML(Number(d.email_confidence || 0).toFixed(0)) + ' · 来源: ' +
      escapeHTML(d.email_source || '未知') + '</div>'
    : '<span style="color:var(--text-secondary);font-size:11px">未找到经过归属验证的邮箱</span>';
  var phoneHTML = d.phone
    ? '<div style="font-size:13px;color:#60a5fa;margin-bottom:4px">' + escapeHTML(d.phone) + '</div>' +
      '<div style="font-size:10px;color:var(--text-secondary)">可信度: ' +
      escapeHTML(Number(d.phone_confidence || 0).toFixed(0)) + ' · 来源: ' +
      escapeHTML(d.phone_source || '未知') + '</div>'
    : '<span style="color:var(--text-secondary);font-size:11px">官方页面未公开电话</span>';
  var institutionSitesHTML = (d.institution_sites || []).map(function(site) {
    var url = /^https?:\/\//.test(site.homepage_url || '') ? site.homepage_url : '#';
    return '<div class="candidate-card"><div class="candidate-head"><a href="' +
      escapeHTML(url) + '" target="_blank" rel="noopener">' +
      escapeHTML(site.institution || site.domain || '机构官网') + '</a></div>' +
      '<div class="candidate-affiliation">' + escapeHTML(site.domain || '') + '</div></div>';
  }).join('') || '<span class="detail-muted">未解析到机构官网</span>';
  var statusLabels = {
    accepted: '已采用', rejected: '已拒绝', review: '待复核',
    'no-results': '无结果', 'no-verified-contact': '无可验证联系方式',
    skipped: '已跳过', unavailable: '不可访问', results: '有结果',
    'profile-found': '找到主页', 'author-matched': '姓名已匹配',
    'name-not-found': '未匹配姓名', completed: '已完成'
  };
  var contactKindLabels = {
    personal: '本人', advisor: '导师/他人', 'advisor/office': '导师/办公室',
    'group/public': '课题组/公共账号', unverified: '归属未确认'
  };
  var contactCandidatesHTML = (d.contact_candidates || []).slice(0, 20).map(function(candidate) {
    var accepted = candidate.status === 'accepted';
    var candidateColor = accepted ? '#4ade80' : (candidate.status === 'rejected' ? '#f87171' : '#facc15');
    var source = /^https?:\/\//.test(candidate.source || '')
      ? '<a href="' + escapeHTML(candidate.source) + '" target="_blank" rel="noopener">来源页面</a>'
      : escapeHTML(candidate.source || '来源未记录');
    return '<div class="candidate-card contact-evidence-card">' +
      '<div class="candidate-head"><span>' + escapeHTML(candidate.type === 'phone' ? '电话' : '邮箱') +
      ' · ' + escapeHTML(contactKindLabels[candidate.contact_kind] || candidate.contact_kind || '归属未确认') +
      '</span><strong style="color:' + candidateColor + '">' +
      escapeHTML(statusLabels[candidate.status] || candidate.status || '待复核') +
      ' · ' + escapeHTML(candidate.confidence || 0) + '分</strong></div>' +
      '<div class="contact-value">' + escapeHTML(candidate.value || '') + '</div>' +
      '<div class="candidate-affiliation">' + escapeHTML(candidate.reason || '未记录判断原因') + '</div>' +
      '<div class="candidate-affiliation">' + source +
      (candidate.department ? ' · ' + escapeHTML(candidate.department) : '') + '</div>' +
      (candidate.context ? '<div class="contact-context">上下文：' + escapeHTML(candidate.context) + '</div>' : '') +
      '</div>';
  }).join('') || '<span class="detail-muted">没有联系信息候选</span>';
  var searchTrailHTML = (d.search_trail || []).slice(0, 60).map(function(item) {
    var label = item.path || item.institution || item.domain || item.stage || '搜索步骤';
    var detail = statusLabels[item.status] || item.status || '';
    if (item.pages_read !== undefined) detail += ' · ' + item.pages_read + '页';
    if (item.results !== undefined) detail += ' · ' + item.results + '条';
    if (item.rejected) detail += ' · 拒绝' + item.rejected + '条';
    if (item.department) detail += ' · ' + item.department;
    var url = /^https?:\/\//.test(item.url || '') ? item.url : '';
    return '<div class="search-trail-item"><div><strong>' + escapeHTML(label) +
      '</strong>：' + escapeHTML(detail) +
      (url ? ' · <a href="' + escapeHTML(url) + '" target="_blank" rel="noopener">页面</a>' : '') +
      '</div>' +
      (item.reason ? '<div>' + escapeHTML(item.reason) + '</div>' : '') +
      (item.query ? '<div class="search-query">检索式：' + escapeHTML(item.query) + '</div>' : '') +
      '</div>';
  }).join('') || '<span class="detail-muted">暂无官网搜索轨迹</span>';
  var graduationHTML = '<div class="score-row"><span>当前阶段</span><strong>' +
    escapeHTML(careerStageLabel(d.career_stage)) + '</strong></div>' +
    '<div class="score-row"><span>毕业判断</span><strong>' +
    escapeHTML(graduationStatusLabel(d.graduation_status)) + '</strong></div>' +
    '<div class="score-row"><span>毕业评分</span><strong>' +
    escapeHTML(Number(d.graduation_score || 0).toFixed(0)) + '</strong></div>' +
    '<div class="score-row"><span>预计年份</span><strong>' +
    escapeHTML(d.expected_graduation_year || '未确认') + '</strong></div>' +
    evidenceListHTML(d.graduation_evidence, '未找到学生身份或毕业时间证据') +
    '<div class="score-row"><span>是否学生</span><strong>' +
    escapeHTML(studentStatusLabel(d.student_status)) + ' · ' +
    escapeHTML(Number(d.student_score || 0).toFixed(0)) + '</strong></div>' +
    evidenceListHTML(d.student_evidence, '暂无独立学生身份证据');
  var chinaHTML = '<div class="score-row"><span>中国关联度</span><strong>' +
    escapeHTML(d.china_link_status || 'none') + ' · ' +
    escapeHTML(Number(d.china_link_score || 0).toFixed(0)) + '</strong></div>' +
    '<div class="score-row"><span>公开国籍依据</span><strong>' +
    escapeHTML(d.nationality === 'China' ? '已明确：中国' : '未确认') + '</strong></div>' +
    '<div class="detail-muted" style="margin:4px 0">中国关联来自教育或机构证据，不按姓名推断国籍。</div>' +
    evidenceListHTML([].concat(d.china_link_evidence || [], d.nationality_evidence || []), '暂无中国教育、机构或明确国籍证据');
  var timeline = d.academic_timeline || {};
  var timelineHTML = '<div class="score-row"><span>最早论文年份</span><strong>' +
    escapeHTML(timeline.earliest_publication_year || '未知') + '</strong></div>' +
    '<div class="score-row"><span>最新论文年份</span><strong>' +
    escapeHTML(timeline.latest_publication_year || '未知') + '</strong></div>' +
    '<div class="score-row"><span>近三年第一作者</span><strong>' +
    escapeHTML(timeline.recent_first_author_count || 0) + '</strong></div>';
  var graduateCandidatesHTML = (d.graduate_candidates || []).map(function(candidate) {
    var profileUrl = /^https?:\/\//.test(candidate.profile_url || '') ? candidate.profile_url : '';
    return '<div class="candidate-card"><div class="candidate-head">' +
      (profileUrl ? '<a href="' + escapeHTML(profileUrl) + '" target="_blank" rel="noopener">' +
        escapeHTML(candidate.name || '学生候选') + '</a>' : '<span>' + escapeHTML(candidate.name || '学生候选') + '</span>') +
      '<strong>' + escapeHTML(careerStageLabel(candidate.career_stage)) + '</strong></div>' +
      '<div class="candidate-affiliation">' + escapeHTML(candidate.institution || '') +
      (candidate.email ? ' · ' + escapeHTML(candidate.email) : '') +
      ' · 官网候选，尚未完成论文身份验证</div></div>';
  }).join('') || '<span class="detail-muted">本次官网路径未发现其他学生成员</span>';
  var evidenceHTML = (d.sources || []).map(function(s) {
    var m = s.match(/(https?:\/\/[^\s]+)/);
    if (m) return '<div style="font-size:10px">' + s.substring(0, s.indexOf(m[0])) + '<a href="' + m[0] + '" target="_blank" style="color:var(--accent);word-break:break-all">' + m[0] + '</a></div>';
    return '<div style="font-size:10px;color:var(--text-secondary);word-break:break-all">' + s + '</div>';
  }).join('') || '<span style="color:var(--text-secondary);font-size:11px">无数据</span>';
  content.innerHTML = '<div class="detail-name">' + escapeHTML(d.name) + matchBadge + '</div>' +
    '<div class="detail-section"><h4>身份匹配</h4>' + breakdownHTML + '</div>' +
    '<div class="detail-section"><h4>用于官网检索的姓名变体</h4>' + aliasesHTML + '</div>' +
    '<div class="detail-section"><h4>毕业候选判断</h4>' + graduationHTML + '</div>' +
    '<div class="detail-section"><h4>中国关联证据</h4>' + chinaHTML + '</div>' +
    '<div class="detail-section"><h4>学术时间线</h4>' + timelineHTML + '</div>' +
    '<div class="detail-section"><h4>官网发现的学生候选</h4>' + graduateCandidatesHTML + '</div>' +
    '<div class="detail-section"><h4>OpenAlex 候选</h4>' + candidatesHTML + '</div>' +
    '<div class="detail-section"><h4>邮箱</h4>' + emailHTML + '</div>' +
    '<div class="detail-section"><h4>公开电话</h4>' + phoneHTML + '</div>' +
    '<div class="detail-section"><h4>机构</h4>' + affHTML + '</div>' +
    '<div class="detail-section"><h4>机构官网路径</h4>' + institutionSitesHTML + '</div>' +
    '<div class="detail-section"><h4>联系信息候选</h4>' + contactCandidatesHTML + '</div>' +
    '<div class="detail-section"><h4>完整联系方式搜索轨迹</h4>' + searchTrailHTML + '</div>' +
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
['show-coauthor', 'show-affiliation', 'show-topic', 'show-lab'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    if (state.graph) renderGraph(state.graph);
  });
});
['show-graduating-only', 'show-china-linked-only', 'show-students-only'].forEach(id => {
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

console.log('GQI Talent Radar ready');

document.getElementById('download-csv-btn').addEventListener('click', async () => {
  if (!state.profiles || state.profiles.length === 0) {
    alert('没有可导出的数据，请先完成爬取');
    return;
  }
  try {
    const data = await api('POST', '/api/export-csv', { profiles: state.profiles });
    const saved = await saveCsvBlob(
      new Blob([data], { type: 'text/csv;charset=utf-8;' }),
      'authors_contacts.csv'
    );
    if (saved) setStatus('CSV 已保存', 'status-done');
  } catch (e) {
    alert('CSV 导出失败: ' + e.message);
  }
});

// History panel
async function loadHistory() {
  const list = document.getElementById('history-list');
  try {
    const projects = await api('GET', '/api/projects');
    state.savedProjects = projects || [];
    if (!projects || projects.length === 0) {
      list.innerHTML = '<div class="history-empty">暂无历史记录<br><span style="font-size:10px">爬取完成后自动保存</span></div>';
      document.getElementById('download-csv-btn').style.display = 'none';
      return;
    }
    list.innerHTML = projects.map(p => `
      <div class="history-item" data-id="${p.id}">
        <div class="hi-title">${escapeHTML(p.title || p.source_info || p.doi || p.id)}</div>
        <div class="hi-meta">
          <span>${escapeHTML(p.created_at || '')}</span>
          <span>${p.source_type === 'contact-merge' ? '联系方式合并' : escapeHTML(p.source_type || '')}</span>
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
    renderCurrentProject(data.project, data.source_projects || []);
    clearGraphSearch(false);
    // Show authors
    if (data.authors && data.authors.length > 0 && !data.summary_only) {
      renderAuthorList(data.authors.map(a => a.name));
      document.getElementById('start-crawl-btn').style.display = 'none';
    }
    // Contact-merge projects now have a real multi-paper provenance graph and
    // use the same explicit history entry point as ordinary paper projects.
    if (data.summary_only) {
      state.graph = null;
      if (g) g.selectAll('*').remove();
      document.getElementById('detail-panel').style.display = 'none';
      document.getElementById('empty-state').style.display = 'flex';
      document.querySelector('#empty-state p').textContent =
        `已合并 ${data.authors.length} 位联系人，可直接点击历史记录中的 CSV 导出`;
      document.getElementById('network-stats').style.display = 'none';
      document.getElementById('author-list-container').style.display = 'none';
      document.getElementById('crawl-action-panel').style.display = 'none';
    } else if (data.graph) {
      state.graph = data.graph;
      renderGraph(data.graph);
      renderStats(data.graph.stats || {});
      document.getElementById('empty-state').style.display = 'none';
      document.getElementById('network-stats').style.display = 'block';
      document.getElementById('download-csv-btn').style.display = 'block';
      if (data.contact_merge) {
        document.getElementById('crawl-action-panel').style.display = 'none';
      }
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

function projectSourceURL(project) {
  if (!project) return '';
  const doi = String(project.doi || '').trim()
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '')
    .replace(/^doi:\s*/i, '');
  if (doi) return `https://doi.org/${encodeURI(doi)}`;
  const source = String(project.source_info || '').trim();
  if (/^https?:\/\//i.test(source)) return source;
  if (/^W\d+$/i.test(source)) return `https://openalex.org/${source}`;
  return '';
}

function copyPlainText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
  return Promise.resolve();
}

function renderCurrentProject(project, sourceProjects = []) {
  const banner = document.getElementById('current-project-banner');
  if (!project) {
    banner.style.display = 'none';
    return;
  }
  const isMerge = project.source_type === 'contact-merge';
  const title = project.title || project.doi || project.source_info || project.id;
  document.getElementById('current-project-title').textContent = title;
  document.getElementById('current-project-meta').textContent = isMerge
    ? `联系方式汇总 · ${sourceProjects.length} 篇论文 · 任务 ID：${project.id}`
    : `${project.source_type || '论文'} · 任务 ID：${project.id}`;
  const sourceLink = document.getElementById('current-project-source');
  const sourceURL = isMerge ? '' : projectSourceURL(project);
  sourceLink.style.display = sourceURL ? '' : 'none';
  if (sourceURL) sourceLink.href = sourceURL;

  const paperToggle = document.getElementById('toggle-source-papers');
  const paperList = document.getElementById('current-project-papers');
  paperList.style.display = 'none';
  paperToggle.style.display = sourceProjects.length ? '' : 'none';
  paperToggle.textContent = `查看 ${sourceProjects.length} 篇论文`;
  paperList.innerHTML = sourceProjects.map(sourceProject => {
    const url = projectSourceURL(sourceProject);
    return `<div class="current-project-paper"><span title="${escapeHTML(sourceProject.title || sourceProject.id)}">${escapeHTML(sourceProject.title || sourceProject.id)}</span>` +
      (url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener">来源</a>` : '') + '</div>';
  }).join('');
  banner.style.display = 'flex';
}

document.getElementById('copy-project-title').addEventListener('click', async () => {
  const title = document.getElementById('current-project-title').textContent.trim();
  if (!title) return;
  await copyPlainText(title);
  const button = document.getElementById('copy-project-title');
  button.textContent = '已复制';
  setTimeout(() => { button.textContent = '复制题名'; }, 1200);
});
document.getElementById('toggle-source-papers').addEventListener('click', () => {
  const list = document.getElementById('current-project-papers');
  list.style.display = list.style.display === 'none' ? 'block' : 'none';
});

async function downloadProjectCSV(projectId) {
  try {
    const res = await fetch(API + '/api/projects/' + projectId + '/csv');
    if (!res.ok) throw new Error('Download failed');
    await saveCsvBlob(await res.blob(), projectId + '.csv');
  } catch (e) {
    alert('CSV 下载失败: ' + e.message);
  }
}

function selectedMergeProjects() {
  return Array.from(state.mergeSelectedProjectIds);
}

function refreshMergeSelection() {
  const selected = selectedMergeProjects();
  document.getElementById('merge-selection-count').textContent = selected.length
    ? `已选择 ${selected.length} 篇论文`
    : '请选择需要合并的论文';
  document.getElementById('merge-create-btn').disabled = selected.length === 0;
}

function mergeBatchKey(project) {
  const match = String(project.id || '').match(/^(.*)-paper-\d+$/);
  return match ? match[1] : '';
}

function mergeBatchLabel(batchId, projects) {
  const rows = projects.filter(project => mergeBatchKey(project) === batchId);
  const idDate = batchId.match(/(?:history-|batch-)?(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
  const first = rows[rows.length - 1] || {};
  const date = idDate
    ? `${idDate[1]}-${idDate[2]}-${idDate[3]} ${idDate[4]}:${idDate[5]}`
    : String(first.created_at || '').slice(0, 16);
  const historical = batchId.startsWith('history-') ? '历史回溯' : '批量任务';
  return `${historical} ${date} · ${rows.length} 篇`;
}

function filteredMergeProjects() {
  const query = document.getElementById('merge-search').value.trim().toLocaleLowerCase();
  const batch = document.getElementById('merge-batch-filter').value;
  const sourceType = document.getElementById('merge-type-filter').value;
  const dateFrom = document.getElementById('merge-date-from').value;
  const dateTo = document.getElementById('merge-date-to').value;
  return (state.savedProjects || []).filter(project => {
    if (project.source_type === 'contact-merge') return false;
    if (batch && mergeBatchKey(project) !== batch) return false;
    if (sourceType === 'historical' && !String(project.id || '').startsWith('history-')) return false;
    if (sourceType === 'pdf' && !['pdf', 'paper-search-pdf'].includes(project.source_type)) return false;
    if (sourceType === 'doi' && project.source_type !== 'doi') return false;
    if (sourceType === 'text' && project.source_type !== 'text') return false;
    const createdDate = String(project.created_at || '').slice(0, 10);
    if (dateFrom && createdDate < dateFrom) return false;
    if (dateTo && createdDate > dateTo) return false;
    if (query) {
      const haystack = [project.title, project.doi, project.source_info, project.id, project.source_type]
        .filter(Boolean).join(' ').toLocaleLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
}

function renderMergeProjectList() {
  const projects = filteredMergeProjects();
  const list = document.getElementById('merge-project-list');
  document.getElementById('merge-filter-count').textContent = `当前筛选 ${projects.length} 篇`;
  list.innerHTML = projects.map(project => `<label class="merge-project-item">` +
    `<input class="merge-project-select" type="checkbox" value="${escapeHTML(project.id)}" ${state.mergeSelectedProjectIds.has(project.id) ? 'checked' : ''}>` +
    `<span title="${escapeHTML(project.title || project.source_info || project.id)}">${escapeHTML(project.title || project.source_info || project.id)}` +
    `<small>${escapeHTML(project.created_at || '')} · ${escapeHTML(project.source_type || '')} · ${escapeHTML(project.id)}</small></span></label>`
  ).join('') || '<div class="history-empty">当前条件下没有论文记录</div>';
  list.querySelectorAll('.merge-project-select').forEach(input => input.addEventListener('change', () => {
    if (input.checked) state.mergeSelectedProjectIds.add(input.value);
    else state.mergeSelectedProjectIds.delete(input.value);
    refreshMergeSelection();
  }));
  refreshMergeSelection();
}

function openMergeBuilder() {
  const projects = (state.savedProjects || []).filter(project => project.source_type !== 'contact-merge');
  state.mergeSelectedProjectIds = new Set();
  const batchSelect = document.getElementById('merge-batch-filter');
  const batchIds = Array.from(new Set(projects.map(mergeBatchKey).filter(Boolean)));
  batchSelect.innerHTML = '<option value="">全部批次</option>' + batchIds.map(batchId =>
    `<option value="${escapeHTML(batchId)}">${escapeHTML(mergeBatchLabel(batchId, projects))}</option>`
  ).join('');
  document.getElementById('merge-search').value = '';
  document.getElementById('merge-type-filter').value = '';
  document.getElementById('merge-date-from').value = '';
  document.getElementById('merge-date-to').value = '';
  document.getElementById('merge-task-name').value = `联系方式汇总 ${localISODate(new Date())}`;
  document.getElementById('merge-builder').style.display = 'block';
  renderMergeProjectList();
}

async function createMergeTask() {
  const projectIds = selectedMergeProjects();
  const name = document.getElementById('merge-task-name').value.trim();
  if (!name) return alert('请输入合并任务名称');
  const button = document.getElementById('merge-create-btn');
  button.disabled = true;
  button.textContent = '正在合并...';
  setStatus('正在创建合并任务...', 'status-running');
  try {
    const result = await api('POST', '/api/contact-merges', {name, project_ids: projectIds});
    document.getElementById('merge-builder').style.display = 'none';
    await loadHistory();
    await loadProject(result.id);
    setStatus(`合并完成：${result.contact_count} 位联系人`, 'status-done');
  } catch (e) {
    setStatus('合并任务创建失败', 'status-error');
    alert('创建合并任务失败: ' + e.message);
  } finally {
    button.textContent = '创建合并任务';
    refreshMergeSelection();
  }
}

document.getElementById('refresh-history-btn').addEventListener('click', loadHistory);
document.getElementById('merge-contacts-btn').addEventListener('click', openMergeBuilder);
document.getElementById('merge-cancel-btn').addEventListener('click', () => {
  document.getElementById('merge-builder').style.display = 'none';
});
['merge-search', 'merge-batch-filter', 'merge-type-filter', 'merge-date-from', 'merge-date-to'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'merge-search' ? 'input' : 'change', renderMergeProjectList);
});
document.getElementById('merge-select-visible').addEventListener('click', () => {
  filteredMergeProjects().forEach(project => state.mergeSelectedProjectIds.add(project.id));
  renderMergeProjectList();
});
document.getElementById('merge-clear-selection').addEventListener('click', () => {
  state.mergeSelectedProjectIds.clear();
  renderMergeProjectList();
});
document.getElementById('merge-create-btn').addEventListener('click', createMergeTask);

// Load history on startup and after crawl completes
loadHistory();
restoreLastBatch();
restoreLastExpansion();

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
