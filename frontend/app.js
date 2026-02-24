/* ============================================================
   DocGuard AI — Frontend Application
   ============================================================ */

const API_BASE = '';

// ---- State ------------------------------------------------------------------
let currentPage = 'dashboard';
let currentDocId = null;
let currentBatchId = null;
let currentBatchDocIds = [];
let trendChart = null;
let processingInterval = null;
let agentStepInterval = null;

// ---- Navigation -------------------------------------------------------------
function navigateTo(page, docId) {
    // Hide all pages
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

    // Show target
    const el = document.getElementById(`page-${page}`);
    if (el) el.classList.add('active');
    const nav = document.getElementById(`nav-${page}`);
    if (nav) nav.classList.add('active');

    currentPage = page;

    // Page-specific init
    switch (page) {
        case 'dashboard': loadDashboard(); break;
        case 'upload': resetUpload(); break;
        case 'documents': loadDocuments(); break;
        case 'standards': loadStandardsPage(); break;
        case 'history': loadHistory(); break;
        case 'analysis':
            if (docId) {
                // If the doc is not part of the current batch, clear batch context
                if (currentBatchId && !currentBatchDocIds.includes(docId)) {
                    currentBatchId = null;
                    currentBatchDocIds = [];
                }
                loadAnalysis(docId, currentBatchId);
            } else if (currentDocId) {
                // Re-load the last viewed document (e.g. sidebar click)
                loadAnalysis(currentDocId, currentBatchId);
            } else {
                // No doc context — fetch the most recent document
                fetchLatestDocForAnalysis();
            }
            break;
        case 'chat': loadChatDocs(); break;
    }
}

// Sidebar nav
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', e => {
        e.preventDefault();
        navigateTo(item.dataset.page);
    });
});

// ============================================================
// DASHBOARD
// ============================================================
async function loadDashboard() {
    try {
        const [docsRes, trendsRes, statsRes] = await Promise.all([
            fetch(`${API_BASE}/api/documents`),
            fetch(`${API_BASE}/api/trends`),
            fetch(`${API_BASE}/api/stats`),
        ]);
        const docsData = await docsRes.json();
        const trendsData = await trendsRes.json();
        const statsData = await statsRes.json();

        const docs = docsData.documents || [];
        const trends = trendsData.trends || [];

        // Stats
        document.getElementById('statTotalDocs').textContent = docs.length;
        document.getElementById('statSaved').textContent = docs.filter(d => d.is_saved).length;

        if (trends.length) {
            const avg = Math.round(trends.reduce((s, t) => s + (t.compliance_score || 0), 0) / trends.length);
            document.getElementById('statAvgScore').textContent = avg;
        }

        // Count high risks
        let highRisk = 0;
        for (const doc of docs) {
            if (doc.has_analysis && doc.status === 'completed') {
                try {
                    const aRes = await fetch(`${API_BASE}/api/analysis/${doc.id}`);
                    const aData = await aRes.json();
                    if (aData.analysis) {
                        highRisk += (aData.analysis.compliance_findings || []).filter(f => f.severity === 'high').length;
                        highRisk += (aData.analysis.security_findings || []).filter(f => f.severity === 'high').length;
                    }
                } catch (_) { }
            }
        }
        document.getElementById('statHighRisk').textContent = highRisk;

        // Lifetime tokens
        const lt = statsData.lifetime_tokens || 0;
        document.getElementById('statLifetimeTokens').textContent = lt > 0 ? lt.toLocaleString() : '0';

        // Trend chart
        renderTrendChart(trends);

        // Recent list
        const recentEl = document.getElementById('recentList');
        const emptyEl = document.getElementById('recentEmpty');
        if (docs.length === 0) {
            recentEl.innerHTML = '';
            emptyEl.style.display = '';
        } else {
            emptyEl.style.display = 'none';
            recentEl.innerHTML = docs.slice(0, 6).map(d => `
                <div class="recent-item" onclick="navigateTo('analysis', ${d.id})">
                    <div class="ri-icon"><i class="fas fa-file-lines"></i></div>
                    <div class="ri-info">
                        <div class="ri-name">${escHtml(d.filename)}</div>
                        <div class="ri-meta">${d.document_type} &bull; ${formatDate(d.upload_date)}</div>
                    </div>
                    <span class="doc-status ${d.status}">${d.status}</span>
                </div>
            `).join('');
        }
    } catch (e) {
        console.error('Dashboard load error', e);
    }
}

function renderTrendChart(trends) {
    const ctx = document.getElementById('trendChart');
    const emptyEl = document.getElementById('trendEmpty');

    if (!trends.length) {
        if (trendChart) { trendChart.destroy(); trendChart = null; }
        ctx.style.display = 'none';
        emptyEl.style.display = '';
        return;
    }

    ctx.style.display = '';
    emptyEl.style.display = 'none';

    const labels = trends.map(t => t.filename ? t.filename.substring(0, 20) : t.date?.substring(0, 10));
    const datasets = [
        { label: 'Overall', data: trends.map(t => t.overall_score), borderColor: '#8b5cf6', backgroundColor: 'rgba(139,92,246,.1)', fill: true, tension: .4 },
        { label: 'Compliance', data: trends.map(t => t.compliance_score), borderColor: '#06b6d4', backgroundColor: 'transparent', tension: .4 },
        { label: 'Security', data: trends.map(t => t.security_score), borderColor: '#10b981', backgroundColor: 'transparent', tension: .4 },
        { label: 'Risk', data: trends.map(t => t.risk_score), borderColor: '#f59e0b', backgroundColor: 'transparent', tension: .4 },
    ];

    if (trendChart) trendChart.destroy();
    trendChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { min: 0, max: 100, ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,.04)' } },
                x: { ticks: { color: '#64748b', maxRotation: 30 }, grid: { display: false } },
            },
            plugins: {
                legend: { labels: { color: '#94a3b8', usePointStyle: true, padding: 16 } },
            },
        },
    });
}

// ============================================================
// UPLOAD
// ============================================================
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const uploadMeta = document.getElementById('uploadMeta');
let selectedFiles = [];

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
    e.preventDefault();
    uploadZone.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFiles(Array.from(e.dataTransfer.files));
});
fileInput.addEventListener('change', () => {
    if (fileInput.files.length) handleFiles(Array.from(fileInput.files));
});

function handleFiles(files) {
    const validExts = ['pdf', 'docx', 'doc', 'txt', 'xlsx', 'xls', 'csv'];
    for (const file of files) {
        const ext = file.name.split('.').pop().toLowerCase();
        if (!validExts.includes(ext)) {
            alert(`Unsupported format: ${file.name}. Use PDF, DOCX, DOC, XLSX, CSV, or TXT.`);
            continue;
        }
        if (file.size > 16 * 1024 * 1024) {
            alert(`File too large: ${file.name}. Max 16 MB.`);
            continue;
        }
        if (selectedFiles.length >= 10) {
            alert('Maximum 10 files per batch.');
            break;
        }
        // Deduplicate by name
        if (!selectedFiles.find(f => f.name === file.name)) {
            selectedFiles.push(file);
        }
    }
    renderFileList();
}

function renderFileList() {
    const list = document.getElementById('selectedFilesList');
    if (selectedFiles.length === 0) {
        uploadZone.style.display = '';
        uploadMeta.style.display = 'none';
        return;
    }
    uploadZone.style.display = 'none';
    uploadMeta.style.display = '';

    const btnText = document.getElementById('analyzeBtnText');
    if (selectedFiles.length === 1) {
        btnText.textContent = 'Start AI Analysis';
    } else {
        btnText.textContent = `Analyze ${selectedFiles.length} Documents`;
    }

    list.innerHTML = `<div class="files-count-badge"><i class="fas fa-files"></i> ${selectedFiles.length} file${selectedFiles.length > 1 ? 's' : ''} selected</div>`;
    selectedFiles.forEach((file, idx) => {
        const item = document.createElement('div');
        item.className = 'selected-file-item';
        item.innerHTML = `
            <i class="fas fa-file-lines"></i>
            <div class="file-info">
                <span class="file-name">${file.name}</span>
                <span class="file-size">${formatBytes(file.size)}</span>
            </div>
            <button class="btn-icon-only" title="Remove" data-idx="${idx}"><i class="fas fa-xmark"></i></button>
        `;
        item.querySelector('button').addEventListener('click', () => {
            selectedFiles.splice(idx, 1);
            renderFileList();
        });
        list.appendChild(item);
    });
}

function resetUpload() {
    selectedFiles = [];
    fileInput.value = '';
    uploadZone.style.display = '';
    uploadMeta.style.display = 'none';
    document.getElementById('processingOverlay').style.display = 'none';
    document.getElementById('batchProgress').style.display = 'none';
    if (processingInterval) clearInterval(processingInterval);
    if (agentStepInterval) clearInterval(agentStepInterval);
    processingInterval = null;
    agentStepInterval = null;
}

// Helper: fetch the most recent document and navigate to its analysis
async function fetchLatestDocForAnalysis() {
    try {
        const res = await fetch(`${API_BASE}/api/documents`);
        const data = await res.json();
        const docs = (data.documents || []).filter(d => d.status === 'completed' && d.has_analysis);
        if (docs.length > 0) {
            // docs are ordered by upload_date desc from API, pick the most recent
            loadAnalysis(docs[0].id, null);
        }
    } catch (e) {
        console.error('Failed to fetch latest doc for analysis:', e);
    }
}

// Radio pills
document.querySelectorAll('.pill').forEach(pill => {
    pill.addEventListener('click', () => {
        document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
    });
});

// Analyze button
document.getElementById('analyzeBtn').addEventListener('click', async () => {
    if (selectedFiles.length === 0) return;

    const docType = document.querySelector('input[name="docType"]:checked').value;

    if (selectedFiles.length === 1) {
        // Single file upload (original flow)
        await uploadSingle(selectedFiles[0], docType);
    } else {
        // Batch upload
        await uploadBatch(selectedFiles, docType);
    }
});

async function uploadSingle(file, docType) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('document_type', docType);

    document.getElementById('processingOverlay').style.display = 'flex';
    document.getElementById('processingTitle').textContent = 'AI Agents Analyzing Document…';
    document.getElementById('processingStatus').textContent = 'Uploading document…';
    document.querySelectorAll('.agent-step').forEach(s => s.classList.remove('active', 'done'));

    try {
        const res = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: fd });
        const data = await res.json();

        if (!res.ok) {
            alert(data.error || 'Upload failed');
            resetUpload();
            return;
        }

        const docId = data.document_id;
        document.getElementById('processingStatus').textContent = 'AI agents are analyzing…';

        // Animate agent steps
        let step = 0;
        agentStepInterval = setInterval(() => {
            const steps = document.querySelectorAll('.agent-step');
            if (step < steps.length) {
                if (step > 0) steps[step - 1].classList.remove('active'), steps[step - 1].classList.add('done');
                steps[step].classList.add('active');
                step++;
            } else {
                clearInterval(agentStepInterval);
            }
        }, 3000);

        // Poll status
        processingInterval = setInterval(async () => {
            try {
                const sRes = await fetch(`${API_BASE}/api/documents/${docId}`);
                const sData = await sRes.json();

                if (sData.document.status === 'completed') {
                    clearInterval(processingInterval);
                    clearInterval(agentStepInterval);
                    document.querySelectorAll('.agent-step').forEach(s => {
                        s.classList.remove('active');
                        s.classList.add('done');
                    });
                    document.getElementById('processingStatus').textContent = 'Analysis complete!';
                    setTimeout(() => {
                        resetUpload();
                        // Clear batch context — this is a single doc upload
                        currentBatchId = null;
                        currentBatchDocIds = [];
                        navigateTo('analysis', docId);
                    }, 1200);
                } else if (sData.document.status === 'failed') {
                    clearInterval(processingInterval);
                    clearInterval(agentStepInterval);
                    alert('Analysis failed. Please try again.');
                    resetUpload();
                }
            } catch (_) { }
        }, 3000);
    } catch (e) {
        alert('Upload error: ' + e.message);
        resetUpload();
    }
}

async function uploadBatch(files, docType) {
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    fd.append('document_type', docType);

    document.getElementById('processingOverlay').style.display = 'flex';
    document.getElementById('processingTitle').textContent = `Analyzing ${files.length} Documents…`;
    document.getElementById('processingStatus').textContent = 'Uploading documents…';
    document.querySelectorAll('.agent-step').forEach(s => s.classList.remove('active', 'done'));

    // Show batch progress
    const batchProgress = document.getElementById('batchProgress');
    const batchDocList = document.getElementById('batchDocList');
    batchProgress.style.display = '';
    batchDocList.innerHTML = '';
    files.forEach(f => {
        const item = document.createElement('div');
        item.className = 'batch-doc-item';
        item.innerHTML = `
            <span class="bdi-icon"><i class="fas fa-hourglass"></i></span>
            <span class="bdi-name">${f.name}</span>
            <span class="bdi-status">Queued</span>
        `;
        batchDocList.appendChild(item);
    });

    try {
        const res = await fetch(`${API_BASE}/api/upload-batch`, { method: 'POST', body: fd });
        const data = await res.json();

        if (!res.ok) {
            alert(data.error || 'Batch upload failed');
            resetUpload();
            return;
        }

        const batchId = data.batch_id;
        const docIds = data.document_ids || [];
        document.getElementById('processingStatus').textContent = 'AI agents are analyzing all documents…';

        // Animate agent steps slowly for batch
        let step = 0;
        agentStepInterval = setInterval(() => {
            const steps = document.querySelectorAll('.agent-step');
            if (step < steps.length) {
                if (step > 0) steps[step - 1].classList.remove('active'), steps[step - 1].classList.add('done');
                steps[step].classList.add('active');
                step++;
            } else {
                clearInterval(agentStepInterval);
            }
        }, 5000);

        // Poll batch status
        processingInterval = setInterval(async () => {
            try {
                const sRes = await fetch(`${API_BASE}/api/batch-analysis/${batchId}`);
                const sData = await sRes.json();
                const batch = sData.batch;
                const docs = sData.documents || [];

                // Update per-document progress
                const items = batchDocList.querySelectorAll('.batch-doc-item');
                docs.forEach((doc, idx) => {
                    if (idx < items.length) {
                        const icon = items[idx].querySelector('.bdi-icon');
                        const status = items[idx].querySelector('.bdi-status');
                        if (doc.status === 'completed') {
                            icon.innerHTML = '<i class="fas fa-check-circle"></i>';
                            icon.className = 'bdi-icon done';
                            status.textContent = `Score: ${doc.analysis?.overall_score || '—'}`;
                        } else if (doc.status === 'processing') {
                            icon.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                            icon.className = 'bdi-icon active';
                            status.textContent = 'Processing…';
                        } else if (doc.status === 'failed') {
                            icon.innerHTML = '<i class="fas fa-times-circle"></i>';
                            icon.className = 'bdi-icon';
                            icon.style.color = 'var(--accent-danger)';
                            status.textContent = 'Failed';
                        }
                    }
                });

                if (batch.status === 'completed') {
                    clearInterval(processingInterval);
                    clearInterval(agentStepInterval);
                    document.querySelectorAll('.agent-step').forEach(s => {
                        s.classList.remove('active');
                        s.classList.add('done');
                    });
                    document.getElementById('processingStatus').textContent = `Batch analysis complete! Score: ${batch.overall_score}`;

                    setTimeout(() => {
                        resetUpload();
                        // Store batch context and navigate to first doc
                        currentBatchId = batchId;
                        currentBatchDocIds = docIds;
                        if (docIds.length > 0) {
                            navigateTo('analysis', docIds[0]);
                        } else {
                            navigateTo('documents');
                        }
                    }, 2000);
                } else if (batch.status === 'failed') {
                    clearInterval(processingInterval);
                    clearInterval(agentStepInterval);
                    alert('Batch analysis failed. Please try again.');
                    resetUpload();
                }
            } catch (_) { }
        }, 5000);
    } catch (e) {
        alert('Batch upload error: ' + e.message);
        resetUpload();
    }
}

// ============================================================
// DOCUMENTS
// ============================================================
async function loadDocuments() {
    try {
        const res = await fetch(`${API_BASE}/api/documents`);
        const data = await res.json();
        const docs = data.documents || [];
        const grid = document.getElementById('documentsGrid');
        const empty = document.getElementById('docsEmpty');

        if (!docs.length) {
            grid.innerHTML = '';
            empty.style.display = '';
            return;
        }

        empty.style.display = 'none';
        grid.innerHTML = docs.map(d => `
            <div class="doc-card" data-doc-id="${d.id}" data-doc-name="${escAttr(d.filename)}" onclick="navigateTo('analysis', ${d.id})">
                <div class="doc-card-header">
                    <div class="doc-card-icon"><i class="fas fa-file-lines"></i></div>
                    <div class="doc-card-actions">
                        ${d.is_saved ? '<span class="saved-badge"><i class="fas fa-bookmark"></i> Saved</span>' : ''}
                        <span class="doc-status ${d.status}">${d.status}</span>
                        <button class="btn-icon-only" onclick="event.stopPropagation(); renameDocumentFromCard(this)" title="Rename">
                            <i class="fas fa-pen" style="color:var(--accent-primary);font-size:.8rem"></i>
                        </button>
                        <button class="btn-icon-only" onclick="event.stopPropagation(); deleteDocument(${d.id}, '${escAttr(d.filename)}')" title="Delete document">
                            <i class="fas fa-trash-alt" style="color:var(--accent-danger);font-size:.8rem"></i>
                        </button>
                    </div>
                </div>
                <div class="doc-card-name">${escHtml(d.filename)}</div>
                <div class="doc-card-meta">
                    <span><i class="fas fa-tag"></i> ${d.document_type}</span>
                    <span><i class="fas fa-calendar"></i> ${formatDate(d.upload_date)}</span>
                    ${d.file_size ? `<span><i class="fas fa-weight-hanging"></i> ${formatBytes(d.file_size)}</span>` : ''}
                </div>
                ${d.has_analysis ? '<div class="doc-card-score"><span>View Analysis →</span></div>' : ''}
            </div>
        `).join('');
    } catch (e) {
        console.error('Load docs error', e);
    }
}

// ============================================================
// HISTORY
// ============================================================
async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/api/history`);
        const data = await res.json();
        const items = data.history || [];
        const tbody = document.getElementById('historyListBody');
        const empty = document.getElementById('historyEmpty');
        const table = document.getElementById('historyTable');

        if (!items.length) {
            tbody.innerHTML = '';
            table.style.display = 'none';
            empty.style.display = 'flex';
            empty.style.flexDirection = 'column';
            empty.style.alignItems = 'center';
            return;
        }

        empty.style.display = 'none';
        table.style.display = '';

        tbody.innerHTML = items.map(item => {
            const isBatch = item.type === 'batch';
            const icon = isBatch ? '<i class="fas fa-layer-group text-accent"></i>' : '<i class="fas fa-file-alt text-primary"></i>';
            const riskBadge = `<span class="badge risk-${item.risk_level}">${item.risk_level.toUpperCase()}</span>`;
            const matBadge = `<span class="badge" style="background:rgba(255,255,255,.05)">${item.maturity}</span>`;
            const scoreClass = item.score >= 80 ? 'score-high' : item.score >= 60 ? 'score-med' : 'score-low';

            let onclick = isBatch
                ? `openBatchFromHistory(${item.real_id})`
                : `navigateTo('analysis', ${item.real_id})`;

            return `
                <tr class="history-row" onclick="${onclick}" style="cursor:pointer">
                    <td>${icon} <span style="margin-left:8px">${isBatch ? 'Batch' : 'Single'}</span></td>
                    <td style="font-weight:600">${escHtml(item.title)}</td>
                    <td class="text-muted"><i class="far fa-calendar-alt"></i> ${formatDate(item.date)}</td>
                    <td><div style="display:flex;gap:4px">${riskBadge}${matBadge}</div></td>
                    <td><span class="${scoreClass}" style="font-weight:bold;font-size:1.1rem">${item.score}</span></td>
                    <td onclick="event.stopPropagation()">
                        <button class="btn-icon-only text-danger" onclick="deleteHistoryItem('${item.type}', ${item.real_id})" title="Delete">
                            <i class="fas fa-trash-alt"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.error('Load history error', e);
    }
}

async function deleteHistoryItem(type, realId) {
    if (!confirm(`Are you sure you want to delete this ${type} analysis?`)) return;

    const endpoint = type === 'batch'
        ? `${API_BASE}/api/batch-analysis/${realId}`
        : `${API_BASE}/api/documents/${realId}`;

    try {
        const res = await fetch(endpoint, { method: 'DELETE' });
        if (res.ok) {
            loadHistory();
        } else {
            const err = await res.json();
            alert('Failed to delete: ' + (err.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function openBatchFromHistory(batchId) {
    try {
        const res = await fetch(`${API_BASE}/api/batch-analysis/${batchId}`);
        if (!res.ok) { alert('Failed to load batch'); return; }
        const data = await res.json();
        const docs = data.documents || [];
        if (!docs.length) { alert('No documents in this batch'); return; }

        // Set batch context so loadAnalysis renders batch tabs
        currentBatchId = batchId;
        currentBatchDocIds = docs.map(d => d.id);

        // Navigate to analysis page with first doc
        navigateTo('analysis', docs[0].id);
    } catch (e) {
        alert('Error loading batch: ' + e.message);
    }
}

// ============================================================
// ANALYSIS
// ============================================================
async function loadAnalysis(docId, batchId) {
    currentDocId = docId;
    const content = document.getElementById('analysisContent');
    const empty = document.getElementById('analysisEmpty');
    const batchTabsEl = document.getElementById('batchDocTabs');
    const saveAllBtn = document.getElementById('saveAllBtn');

    // Clean up any leftover cumulative analysis view from a previous batch
    const oldCumView = document.getElementById('cumulativeAnalysisView');
    if (oldCumView) oldCumView.remove();
    // Ensure individual analysis content is visible (cumulative view hides it)
    content.style.display = '';

    try {
        const res = await fetch(`${API_BASE}/api/analysis/${docId}`);
        if (!res.ok) {
            content.style.display = 'none';
            empty.style.display = '';
            batchTabsEl.style.display = 'none';
            saveAllBtn.style.display = 'none';
            return;
        }

        const data = await res.json();
        const doc = data.document;
        const a = data.analysis;

        document.getElementById('analysisDocName').textContent = doc.filename;
        content.style.display = '';
        empty.style.display = 'none';

        // ---- Batch mode: render document tabs ----
        if (batchId && currentBatchDocIds.length > 1) {
            batchTabsEl.style.display = '';
            document.getElementById('batchDocCount').textContent = `${currentBatchDocIds.length} documents`;

            // Fetch batch details for tab names & scores
            try {
                const bRes = await fetch(`${API_BASE}/api/batch-analysis/${batchId}`);
                const bData = await bRes.json();
                const bDocs = bData.documents || [];
                const batch = bData.batch || {};
                const tabsContainer = document.getElementById('batchTabsContainer');
                tabsContainer.innerHTML = '';

                // === Cumulative Analysis tab (first) ===
                const cumTab = document.createElement('div');
                cumTab.className = 'bdt-tab';
                cumTab.innerHTML = `
                    <i class="fas fa-chart-pie"></i>
                    <span>Cumulative Analysis</span>
                    <span class="bdt-score">${batch.overall_score || '—'}</span>
                `;
                cumTab.addEventListener('click', () => {
                    // Mark this tab active
                    tabsContainer.querySelectorAll('.bdt-tab').forEach(t => t.classList.remove('active'));
                    cumTab.classList.add('active');
                    // Hide individual analysis, show cumulative
                    content.style.display = 'none';
                    showCumulativeAnalysis(batch);
                });
                tabsContainer.appendChild(cumTab);

                // === Individual document tabs ===
                bDocs.forEach(bd => {
                    const tab = document.createElement('div');
                    tab.className = `bdt-tab${bd.id === docId ? ' active' : ''}`;
                    const score = bd.analysis?.overall_score || '—';
                    tab.innerHTML = `
                        <i class="fas fa-file-lines"></i>
                        <span>${bd.original_filename || bd.filename || 'Document'}</span>
                        <span class="bdt-score">${score}</span>
                    `;
                    tab.addEventListener('click', () => {
                        // Remove cumulative view, load individual
                        const cumView = document.getElementById('cumulativeAnalysisView');
                        if (cumView) cumView.remove();
                        loadAnalysis(bd.id, batchId);
                    });
                    tabsContainer.appendChild(tab);
                });
            } catch (e) {
                console.error('Failed to load batch tabs:', e);
            }

            // Show Save All, hide single Save
            saveAllBtn.style.display = '';
            saveAllBtn.onclick = () => saveAllBatchDocuments(batchId);
            document.getElementById('saveDocBtn').style.display = 'none';
        } else {
            batchTabsEl.style.display = 'none';
            saveAllBtn.style.display = 'none';

            // Save button (single doc mode)
            const saveBtn = document.getElementById('saveDocBtn');
            if (doc.is_saved) {
                saveBtn.innerHTML = '<i class="fas fa-check"></i> Saved';
                saveBtn.disabled = true;
            } else {
                saveBtn.innerHTML = '<i class="fas fa-bookmark"></i> Save to Knowledge Base';
                saveBtn.disabled = false;
            }
            saveBtn.style.display = '';
            saveBtn.onclick = () => saveDocument(docId);
        }

        // Delete button
        const delBtn = document.getElementById('deleteDocBtn');
        delBtn.style.display = '';
        delBtn.onclick = () => deleteDocument(docId, doc.filename);

        // ---- Hero score ring ----
        // Add SVG gradient
        const heroRing = document.getElementById('heroRing');
        const svg = heroRing.closest('svg');
        if (!svg.querySelector('#ringGrad')) {
            const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
            defs.innerHTML = `<linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#8b5cf6"/>
                <stop offset="100%" style="stop-color:#06b6d4"/>
            </linearGradient>`;
            svg.prepend(defs);
        }

        const score = Math.round(a.overall_score || 0);
        animateNumber('heroScore', score);
        const circumference = 2 * Math.PI * 85;
        heroRing.style.strokeDasharray = circumference;
        setTimeout(() => {
            heroRing.style.strokeDashoffset = circumference - (circumference * score / 100);
        }, 100);

        // Meta
        const rl = (a.risk_level || 'medium').toLowerCase();
        document.getElementById('heroRiskLevel').textContent = rl;
        document.getElementById('heroRiskLevel').className = `meta-value badge badge-${rl}`;

        const mat = a.document_maturity || 'basic';
        document.getElementById('heroMaturity').textContent = mat;
        document.getElementById('heroMaturity').className = 'meta-value badge badge-medium';

        document.getElementById('heroTime').textContent = a.processing_time ? `${a.processing_time}s` : '–';
        const tokens = a.total_tokens ? a.total_tokens.toLocaleString() : '–';
        document.getElementById('heroTokens').textContent = tokens;

        // Score rationale
        const rationaleEl = document.getElementById('heroScoreRationale');
        const rationale = a.score_rationale || [];
        if (rationale.length > 0) {
            rationaleEl.innerHTML = `
                <div class="rationale-header"><i class="fas fa-lightbulb"></i> Score Breakdown</div>
                <ul class="rationale-list">
                    ${rationale.map(r => `<li>${escHtml(r)}</li>`).join('')}
                </ul>
            `;
        } else {
            rationaleEl.innerHTML = '';
        }

        // ---- Score bars ----
        setBar('barCompliance', 'valCompliance', a.compliance_score);
        setBar('barSecurity', 'valSecurity', a.security_score);
        setBar('barRisk', 'valRisk', a.risk_score);
        setBar('barComplete', 'valComplete', a.completeness_score);
        setBar('barStrength', 'valStrength', a.security_strength_score);
        setBar('barCoverage', 'valCoverage', a.coverage_score);
        setBar('barClarity', 'valClarity', a.clarity_score);
        setBar('barEnforce', 'valEnforce', a.enforcement_score);

        // ---- Findings ----
        renderFindings('complianceList', a.compliance_findings || [], 'issue');
        renderFindings('securityList', a.security_findings || [], 'issue');
        renderFindings('riskList', a.risk_findings || [], 'risk');

        // ---- Frameworks ----
        renderFrameworks(a.framework_mappings || {});

        // ---- Gaps ----
        renderGaps(a.gap_detections || []);

        // ---- Best practices ----
        renderBestPractices(a.best_practices || []);

        // ---- Suggestions ----
        renderSuggestions(a.suggestions || []);

        // ---- Recommendations ----
        renderRecommendations(a.recommendations || []);

        // Wire up tabs
        wireAnalysisTabs();

    } catch (e) {
        console.error('Load analysis error', e);
        content.style.display = 'none';
        empty.style.display = '';
    }
}

function setBar(barId, valId, score) {
    const val = Math.round(score || 0);
    const bar = document.getElementById(barId);
    const num = document.getElementById(valId);
    if (bar) setTimeout(() => bar.style.width = val + '%', 100);
    if (num) animateNumber(valId, val);
}

function animateNumber(elId, target) {
    const el = document.getElementById(elId);
    if (!el) return;
    let current = 0;
    const step = target / 40;
    const interval = setInterval(() => {
        current += step;
        if (current >= target) { current = target; clearInterval(interval); }
        el.textContent = Math.round(current);
    }, 25);
}

function renderFindings(containerId, findings, key) {
    const el = document.getElementById(containerId);
    if (!findings.length) {
        el.innerHTML = '<p style="color: var(--text-muted); font-size: .85rem;">No findings</p>';
        return;
    }
    el.innerHTML = findings.map(f => `
        <div class="finding-card sev-${f.severity || 'medium'}">
            <div class="fc-title">${escHtml(f[key] || f.issue || f.risk || '')}</div>
            <div class="fc-detail">
                ${f.section ? `Section: ${escHtml(f.section)}` : ''}
                ${f.category ? `Category: ${escHtml(f.category)}` : ''}
                ${f.type ? `Type: ${escHtml(f.type)}` : ''}
                &bull; Severity: <strong>${f.severity || '?'}</strong>
            </div>
        </div>
    `).join('');
}

async function renderFrameworks(mappings) {
    const el = document.getElementById('frameworksGrid');
    const frameworks = Object.entries(mappings);

    // Check if all frameworks are in "not_uploaded" (pending selection) state
    const allPending = frameworks.length === 0 ||
        frameworks.every(([, data]) => data.not_uploaded);

    if (allPending) {
        showFwSelector(el);
        return;
    }

    // Fetch live upload status for accurate badges (handles legacy data without source field)
    let uploadStatus = {};
    try {
        const res = await fetch(`${API_BASE}/api/frameworks/status`);
        if (res.ok) uploadStatus = await res.json();
    } catch (e) { console.error('Failed to fetch framework status', e); }

    // Separate results from pending
    const results = frameworks.filter(([, data]) => !data.not_uploaded);
    const pending = frameworks.filter(([, data]) => data.not_uploaded);

    let html = '';

    // Render actual results
    results.forEach(([name, data]) => {
        const controls = data.mapped_controls || [];
        // Calculate score from actual control statuses for consistency
        const score = controls.length > 0
            ? Math.round(controls.reduce((sum, c) => {
                const s = c.status || 'not_met';
                return sum + (s === 'met' ? 100 : s === 'partial' ? 50 : 0);
            }, 0) / controls.length)
            : (data.alignment_score || 0);
        const color = score >= 70 ? 'var(--accent-success)' : score >= 40 ? 'var(--accent-warning)' : 'var(--accent-danger)';
        const cardId = `fw-${name.replace(/[^a-zA-Z0-9]/g, '')}`;
        const extraCount = controls.length - 5;

        // Source indicator: show "verified" if stored source says uploaded OR live status confirms uploaded
        const isUploaded = data.source === 'uploaded_standard' || uploadStatus[name] === true;
        const sourceTag = isUploaded
            ? '<div class="fw-source-tag uploaded"><i class="fas fa-database"></i> Verified against uploaded standard</div>'
            : `<div class="fw-source-tag ai-knowledge">
                    <i class="fas fa-robot"></i> Based on AI knowledge
                    <button class="fw-upload-link" onclick="navigateTo('standards')">Upload ${escHtml(name)} Standard →</button>
               </div>`;

        html += `
            <div class="fw-card">
                <div class="fw-card-header">
                    <span class="fw-name">${escHtml(name)}</span>
                    <span class="fw-score" style="color:${color}">${Math.round(score)}%</span>
                </div>
                ${sourceTag}
                ${data.summary ? `<p class="fw-summary">${escHtml(data.summary)}</p>` : ''}
                <div class="fw-controls">
                    ${controls.slice(0, 5).map(c => `
                        <div class="fw-control">
                            <span class="fc-status ${c.status || 'not_met'}"></span>
                            <span>${escHtml(c.control_id || c.article || c.rule || '')} — ${escHtml(c.control_name || c.requirement || '')}</span>
                        </div>
                    `).join('')}
                    ${extraCount > 0 ? `
                        <div class="fw-extra-controls" id="${cardId}-extra" style="display:none;">
                            ${controls.slice(5).map(c => `
                                <div class="fw-control">
                                    <span class="fc-status ${c.status || 'not_met'}"></span>
                                    <span>${escHtml(c.control_id || c.article || c.rule || '')} — ${escHtml(c.control_name || c.requirement || '')}</span>
                                </div>
                            `).join('')}
                        </div>
                        <div class="fw-toggle" id="${cardId}-toggle" onclick="toggleFwControls('${cardId}', ${extraCount})">
                            <i class="fas fa-chevron-down"></i> +${extraCount} more controls
                        </div>
                    ` : ''}
                </div>
            </div>
        `;
    });

    // Add a "Check More Frameworks" button if some are still pending
    if (pending.length > 0) {
        html += `
            <div class="fw-check-more">
                <button class="btn btn-accent btn-sm" onclick="showFwSelector(document.getElementById('frameworksGrid'), true)">
                    <i class="fas fa-plus-circle"></i> Check More Frameworks
                </button>
            </div>
        `;
    }

    el.innerHTML = html;
}

async function showFwSelector(container, keepExisting) {
    const fwMeta = {
        CIS: { name: 'CIS Controls', icon: 'fa-shield-halved', color: '#6366f1' },
        GDPR: { name: 'GDPR', icon: 'fa-euro-sign', color: '#8b5cf6' },
        HIPAA: { name: 'HIPAA', icon: 'fa-hospital', color: '#ec4899' },
        ISO27001: { name: 'ISO 27001', icon: 'fa-certificate', color: '#06b6d4' },
        NIST: { name: 'NIST CSF', icon: 'fa-landmark', color: '#10b981' },
        SOC2: { name: 'SOC 2', icon: 'fa-file-shield', color: '#f59e0b' },
    };

    // Fetch upload status
    let uploadStatus = {};
    try {
        const res = await fetch(`${API_BASE}/api/frameworks/status`);
        if (res.ok) uploadStatus = await res.json();
    } catch (e) { console.error('Failed to fetch framework status', e); }

    const selectorHtml = `
        <div class="fw-selector glass-card" id="fwSelector">
            <div class="fw-selector-header">
                <i class="fas fa-scale-balanced"></i>
                <div>
                    <h3>Select Frameworks to Compare</h3>
                    <p>Choose which compliance frameworks to check this document against</p>
                </div>
            </div>
            <div class="fw-selector-options">
                <label class="fw-selector-option select-all" onclick="toggleSelectAllFw(this)">
                    <input type="checkbox" id="fw-select-all" />
                    <div class="fw-opt-icon" style="background:rgba(255,255,255,.08); color:var(--text-primary)">
                        <i class="fas fa-check-double"></i>
                    </div>
                    <span>Select All Frameworks</span>
                </label>
                ${Object.entries(fwMeta).map(([key, m]) => {
        const uploaded = uploadStatus[key] === true;
        const badge = uploaded
            ? '<span class="fw-upload-badge uploaded"><i class="fas fa-check-circle"></i> Uploaded</span>'
            : '<span class="fw-upload-badge not-uploaded"><i class="fas fa-cloud-arrow-up"></i> Not Uploaded</span>';
        return `
                    <label class="fw-selector-option">
                        <input type="checkbox" value="${key}" class="fw-check" />
                        <div class="fw-opt-icon" style="background:${m.color}20; color:${m.color}">
                            <i class="fas ${m.icon}"></i>
                        </div>
                        <span>${m.name}</span>
                        ${badge}
                    </label>`;
    }).join('')}
            </div>
            <div class="fw-selector-actions">
                <button class="btn btn-primary" id="fwCompareBtn" onclick="runFrameworkCheck()" disabled>
                    <i class="fas fa-magnifying-glass-chart"></i> Compare Selected
                </button>
            </div>
        </div>
    `;

    if (keepExisting) {
        const existingSel = container.querySelector('#fwSelector');
        if (existingSel) existingSel.remove();
        const moreBtn = container.querySelector('.fw-check-more');
        if (moreBtn) moreBtn.remove();
        container.insertAdjacentHTML('beforeend', selectorHtml);
    } else {
        container.innerHTML = selectorHtml;
    }

    // Attach change listeners
    container.querySelectorAll('.fw-check').forEach(cb => {
        cb.addEventListener('change', () => {
            const anyChecked = container.querySelectorAll('.fw-check:checked').length > 0;
            document.getElementById('fwCompareBtn').disabled = !anyChecked;
            const allChecked = container.querySelectorAll('.fw-check:checked').length ===
                container.querySelectorAll('.fw-check').length;
            document.getElementById('fw-select-all').checked = allChecked;
        });
    });
}

function toggleSelectAllFw(label) {
    const selectAll = label.querySelector('input');
    setTimeout(() => {
        const checked = selectAll.checked;
        document.querySelectorAll('.fw-check').forEach(cb => { cb.checked = checked; });
        document.getElementById('fwCompareBtn').disabled = !checked;
    }, 0);
}

async function runFrameworkCheck() {
    if (!currentDocId) return;
    const checks = document.querySelectorAll('.fw-check:checked');
    const selected = Array.from(checks).map(cb => cb.value);
    if (!selected.length) return;

    const btn = document.getElementById('fwCompareBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Comparing\u2026';

    // Show loading cards
    const grid = document.getElementById('frameworksGrid');
    const sel = document.getElementById('fwSelector');
    if (sel) sel.style.display = 'none';

    const fwNames = typeof FRAMEWORK_META !== 'undefined' ? FRAMEWORK_META : {};
    let loadingHtml = '<div class="fw-loading-grid" id="fwLoadingGrid">';
    selected.forEach(key => {
        const meta = fwNames[key] || { name: key, icon: 'fa-file', color: '#888' };
        loadingHtml += `
            <div class="fw-card fw-loading">
                <div class="fw-card-header">
                    <span class="fw-name">${meta.name}</span>
                    <span class="fw-score"><i class="fas fa-spinner fa-spin" style="color:var(--accent-primary)"></i></span>
                </div>
                <div class="fw-loading-body">
                    <div class="fw-loading-bar"></div>
                    <p>Analyzing against ${meta.name}\u2026</p>
                </div>
            </div>
        `;
    });
    loadingHtml += '</div>';
    grid.insertAdjacentHTML('beforeend', loadingHtml);

    try {
        const res = await fetch(`${API_BASE}/api/analysis/${currentDocId}/check-frameworks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frameworks: selected }),
        });

        const data = await res.json();
        if (!res.ok) {
            alert(data.error || 'Framework check failed');
            if (sel) sel.style.display = '';
            const lg = document.getElementById('fwLoadingGrid');
            if (lg) lg.remove();
            btn.innerHTML = '<i class="fas fa-magnifying-glass-chart"></i> Compare Selected';
            btn.disabled = false;
            return;
        }

        renderFrameworks(data.framework_mappings || {});
    } catch (e) {
        console.error('Framework check failed', e);
        alert('Failed to run framework comparison');
        if (sel) sel.style.display = '';
        const lg = document.getElementById('fwLoadingGrid');
        if (lg) lg.remove();
        btn.innerHTML = '<i class="fas fa-magnifying-glass-chart"></i> Compare Selected';
        btn.disabled = false;
    }
}

function toggleFwControls(cardId, count) {
    const extra = document.getElementById(`${cardId}-extra`);
    const toggle = document.getElementById(`${cardId}-toggle`);
    const isHidden = extra.style.display === 'none';
    extra.style.display = isHidden ? 'block' : 'none';
    toggle.innerHTML = isHidden
        ? '<i class="fas fa-chevron-up"></i> Show less'
        : `<i class="fas fa-chevron-down"></i> +${count} more controls`;
}

function renderGaps(gaps) {
    const el = document.getElementById('gapsList');
    if (!gaps.length) {
        el.innerHTML = '<p style="color: var(--text-muted);">No policy gaps detected — the document covers all key areas.</p>';
        return;
    }
    el.innerHTML = gaps.map(g => `
        <div class="gap-card">
            <div class="gap-indicator detected">
                <i class="fas fa-circle-exclamation"></i>
            </div>
            <div class="gap-info">
                <div class="gap-title">${escHtml(g.gap_title || g.gap_type || '')}</div>
                <div class="gap-detail">${escHtml(g.details || '')}</div>
                ${g.recommendation ? `<div class="gap-rec">💡 ${escHtml(g.recommendation)}</div>` : ''}
            </div>
            <span class="doc-status failed">${g.severity || 'detected'}</span>
        </div>
    `).join('');
}

function renderBestPractices(bps) {
    const el = document.getElementById('bpList');
    if (!bps.length) {
        el.innerHTML = '<p style="color: var(--text-muted);">No best practice comparisons available</p>';
        return;
    }
    el.innerHTML = bps.map(bp => `
        <div class="bp-card">
            <div class="bp-area">${escHtml(bp.area || '')}</div>
            <div class="bp-row"><span class="bp-label">Current State:</span><span>${escHtml(bp.current_state || '')}</span></div>
            <div class="bp-row"><span class="bp-label">Best Practice:</span><span>${escHtml(bp.best_practice || '')}</span></div>
            <div class="bp-row bp-gap"><span class="bp-label">Gap:</span><span class="doc-status ${bp.gap === 'high' ? 'failed' : bp.gap === 'medium' ? 'processing' : 'completed'}">${bp.gap || 'none'}</span></div>
            ${bp.recommendation ? `<div class="bp-row" style="margin-top:.3rem;"><span class="bp-label">Action:</span><span style="color:var(--accent-purple);">${escHtml(bp.recommendation)}</span></div>` : ''}
        </div>
    `).join('');
}

function renderSuggestions(suggestions) {
    const el = document.getElementById('suggestionsList');
    if (!suggestions.length) {
        el.innerHTML = '<p style="color: var(--text-muted);">No suggestions available</p>';
        return;
    }
    el.innerHTML = suggestions.map(s => `
        <div class="sug-card">
            <div class="sug-header">
                <span class="sug-type">${escHtml(s.type || '').replace(/_/g, ' ')}</span>
                <span class="sug-priority doc-status ${s.priority === 'high' ? 'failed' : s.priority === 'medium' ? 'processing' : 'completed'}">${s.priority || ''}</span>
            </div>
            <div class="sug-title">${escHtml(s.title || '')}</div>
            <div class="sug-desc">${escHtml(s.description || '')}</div>
            ${s.example_text ? `<div class="sug-example">"${escHtml(s.example_text)}"</div>` : ''}
        </div>
    `).join('');
}

function renderRecommendations(recs) {
    const el = document.getElementById('recsList');
    if (!recs.length) {
        el.innerHTML = '<p style="color: var(--text-muted);">No recommendations available</p>';
        return;
    }
    el.innerHTML = recs.map(r => `
        <div class="rec-card">
            <div class="rec-priority ${r.priority || 'medium'}"></div>
            <div>
                <div class="rec-text">${escHtml(r.action || '')}</div>
                <div class="rec-cat">${r.category || ''}</div>
            </div>
        </div>
    `).join('');
}

// ============================================================
// CUMULATIVE ANALYSIS VIEW (Batch Mode)
// ============================================================
function showCumulativeAnalysis(batch) {
    // Remove any existing cumulative view
    const existing = document.getElementById('cumulativeAnalysisView');
    if (existing) existing.remove();

    const crossDocGaps = batch.cross_doc_gaps || {};
    const synthesis = batch.synthesis || {};
    const coverage = synthesis.coverage_summary || {};
    const resolvedGaps = crossDocGaps.resolved_gaps || [];
    const corpusGaps = crossDocGaps.corpus_gaps || [];
    const contradictions = crossDocGaps.contradictions || [];
    const strengths = synthesis.strengths || [];
    const priorities = synthesis.top_priorities || [];

    const severityColor = (s) => {
        const map = { critical: '#ef4444', high: '#f97316', medium: '#eab308', low: '#22c55e' };
        return map[(s || '').toLowerCase()] || '#94a3b8';
    };

    const statusPill = (status) => {
        const map = {
            covered: { bg: 'rgba(34,197,94,.15)', color: '#22c55e', icon: 'fa-check-circle', label: 'Covered' },
            still_open: { bg: 'rgba(239,68,68,.15)', color: '#ef4444', icon: 'fa-exclamation-circle', label: 'Open' },
            partially_covered: { bg: 'rgba(234,179,8,.15)', color: '#eab308', icon: 'fa-minus-circle', label: 'Partial' },
        };
        const m = map[(status || '').toLowerCase()] || map.still_open;
        return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:600;background:${m.bg};color:${m.color}"><i class="fas ${m.icon}"></i> ${m.label}</span>`;
    };

    // Count resolved vs open
    const coveredCount = resolvedGaps.filter(g => g.status === 'covered').length;
    const openCount = resolvedGaps.filter(g => g.status === 'still_open').length;
    const partialCount = resolvedGaps.filter(g => g.status === 'partially_covered').length;

    const view = document.createElement('div');
    view.id = 'cumulativeAnalysisView';
    view.className = 'cumulative-analysis';
    view.innerHTML = `
        <!-- Hero Section -->
        <div class="glass-card cum-hero">
            <div class="cum-hero-left">
                <div class="cum-score-ring">
                    <svg viewBox="0 0 120 120" width="120" height="120">
                        <circle cx="60" cy="60" r="50" fill="none" stroke="rgba(139,92,246,.15)" stroke-width="8"/>
                        <circle cx="60" cy="60" r="50" fill="none" stroke="url(#cumGrad)" stroke-width="8"
                            stroke-dasharray="${2 * Math.PI * 50}"
                            stroke-dashoffset="${2 * Math.PI * 50 - (2 * Math.PI * 50 * (batch.overall_score || 0) / 100)}"
                            stroke-linecap="round" transform="rotate(-90 60 60)"/>
                        <defs><linearGradient id="cumGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" style="stop-color:#8b5cf6"/><stop offset="100%" style="stop-color:#06b6d4"/>
                        </linearGradient></defs>
                        <text x="60" y="65" text-anchor="middle" font-size="28" font-weight="800" fill="white">${Math.round(batch.overall_score || 0)}</text>
                    </svg>
                </div>
                <div class="cum-hero-meta">
                    <h2 style="margin:0 0 .5rem">Cumulative Score</h2>
                    <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;width:100%;">
                        <span class="cum-badge" style="background:rgba(139,92,246,.15);color:#a78bfa;">Risk: ${(batch.risk_level || 'medium').toUpperCase()}</span>
                        <span class="cum-badge" style="background:rgba(6,182,212,.15);color:#22d3ee;">Maturity: ${(batch.document_maturity || 'developing').toUpperCase()}</span>
                        <span class="cum-badge" style="background:rgba(34,197,94,.15);color:#22c55e;">${currentBatchDocIds.length} Documents</span>
                        <span class="cum-badge" style="background:rgba(249,115,22,.15);color:#f97316;margin-left:auto;"><i class="fas fa-coins" style="margin-right:4px;"></i>${(batch.synthesis?.total_tokens || 0).toLocaleString()} Process Tokens</span>
                    </div>
                    ${(batch.score_rationale && batch.score_rationale.length > 0) ? `
                        <div class="score-rationale" style="margin-top:.75rem;width:100%;">
                            <div class="rationale-header"><i class="fas fa-lightbulb"></i> Score Breakdown</div>
                            <ul class="rationale-list">
                                ${batch.score_rationale.map(r => `<li>${escHtml(r)}</li>`).join('')}
                            </ul>
                        </div>
                    ` : ''}
                </div>
            </div>
            ${synthesis.executive_summary ? `<p class="cum-summary">${synthesis.executive_summary}</p>` : ''}
        </div>

        <!-- Gap Resolution Summary -->
        <div class="glass-card" style="padding:1.5rem;">
            <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                <i class="fas fa-arrows-rotate" style="color:#8b5cf6"></i> Cross-Document Gap Resolution
            </h3>
            <div class="cum-gap-stats">
                <div class="cum-gap-stat">
                    <span class="cum-gap-stat-num" style="color:#22c55e">${coveredCount}</span>
                    <span class="cum-gap-stat-label">Covered by<br>Other Docs</span>
                </div>
                <div class="cum-gap-stat">
                    <span class="cum-gap-stat-num" style="color:#eab308">${partialCount}</span>
                    <span class="cum-gap-stat-label">Partially<br>Covered</span>
                </div>
                <div class="cum-gap-stat">
                    <span class="cum-gap-stat-num" style="color:#ef4444">${openCount}</span>
                    <span class="cum-gap-stat-label">Still<br>Open</span>
                </div>
                <div class="cum-gap-stat">
                    <span class="cum-gap-stat-num" style="color:#f97316">${corpusGaps.length}</span>
                    <span class="cum-gap-stat-label">Org-Wide<br>Gaps</span>
                </div>
            </div>
        </div>

        <!-- Resolved Gaps Detail -->
        ${resolvedGaps.length > 0 ? `
        <div class="glass-card" style="padding:1.5rem;">
            <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                <i class="fas fa-check-double" style="color:#22c55e"></i> Gap Resolution Details
            </h3>
            <div class="cum-resolved-list">
                ${resolvedGaps.map(g => `
                    <div class="cum-resolved-item">
                        <div class="cum-resolved-header">
                            ${statusPill(g.status)}
                            <span class="cum-resolved-title">${g.original_gap || 'Unnamed Gap'}</span>
                        </div>
                        <div class="cum-resolved-meta">
                            <span><i class="fas fa-file-lines"></i> Found in: <strong>${g.source_document || '—'}</strong></span>
                            ${g.covered_by ? `<span><i class="fas fa-shield-check"></i> Covered by: <strong>${g.covered_by}</strong></span>` : ''}
                        </div>
                        ${g.evidence ? `<p class="cum-resolved-evidence">${g.evidence}</p>` : ''}
                    </div>
                `).join('')}
            </div>
        </div>
        ` : ''}

        <!-- Corpus Gaps (true org-wide gaps) -->
        ${corpusGaps.length > 0 ? `
        <div class="glass-card" style="padding:1.5rem;">
            <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                <i class="fas fa-triangle-exclamation" style="color:#f97316"></i> Organization-Wide Gaps
                <span style="font-size:.78rem;color:var(--text-muted);font-weight:400;margin-left:auto;">Not covered by any document</span>
            </h3>
            ${corpusGaps.map(g => `
                <div class="cum-corpus-gap">
                    <div class="cum-corpus-gap-header">
                        <span class="severity-dot" style="background:${severityColor(g.severity)}"></span>
                        <strong>${g.gap_title || 'Unnamed'}</strong>
                        <span class="cum-badge" style="background:${severityColor(g.severity)}22;color:${severityColor(g.severity)};text-transform:uppercase;font-size:.7rem;">${g.severity || 'medium'}</span>
                    </div>
                    <p style="margin:.4rem 0 .2rem;color:var(--text-secondary);font-size:.85rem;">${g.details || ''}</p>
                    ${g.recommendation ? `<p style="margin:.2rem 0 0;font-size:.82rem;color:#a78bfa;"><i class="fas fa-lightbulb"></i> ${g.recommendation}</p>` : ''}
                </div>
            `).join('')}
        </div>
        ` : ''}

        <!-- Contradictions -->
        ${contradictions.length > 0 ? `
        <div class="glass-card" style="padding:1.5rem;">
            <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                <i class="fas fa-code-compare" style="color:#ef4444"></i> Contradictions Between Documents
            </h3>
            ${contradictions.map(c => `
                <div class="cum-contradiction">
                    <div style="font-weight:600;margin-bottom:.5rem;">${c.topic || 'Topic'}</div>
                    <div class="cum-contra-docs">
                        <div class="cum-contra-doc">
                            <span class="cum-contra-label">${c.document_a || 'Doc A'}</span>
                            <p>${c.document_a_says || ''}</p>
                        </div>
                        <i class="fas fa-arrows-left-right" style="color:var(--text-muted);font-size:1.2rem;"></i>
                        <div class="cum-contra-doc">
                            <span class="cum-contra-label">${c.document_b || 'Doc B'}</span>
                            <p>${c.document_b_says || ''}</p>
                        </div>
                    </div>
                    ${c.recommendation ? `<p style="margin:.5rem 0 0;font-size:.82rem;color:#a78bfa;"><i class="fas fa-lightbulb"></i> ${c.recommendation}</p>` : ''}
                </div>
            `).join('')}
        </div>
        ` : ''}

        <!-- Coverage + Strengths side by side -->
        <div class="cum-two-col">
            <div class="glass-card" style="padding:1.5rem;">
                <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                    <i class="fas fa-bullseye" style="color:#06b6d4"></i> Coverage Analysis
                </h3>
                ${(coverage.well_covered_areas || []).length > 0 ? `
                    <div class="cum-coverage-section">
                        <span class="cum-cov-label" style="color:#22c55e"><i class="fas fa-check"></i> Well Covered</span>
                        <ul>${(coverage.well_covered_areas || []).map(a => `<li>${a}</li>`).join('')}</ul>
                    </div>
                ` : ''}
                ${(coverage.weakly_covered_areas || []).length > 0 ? `
                    <div class="cum-coverage-section">
                        <span class="cum-cov-label" style="color:#eab308"><i class="fas fa-minus"></i> Weakly Covered</span>
                        <ul>${(coverage.weakly_covered_areas || []).map(a => `<li>${a}</li>`).join('')}</ul>
                    </div>
                ` : ''}
                ${(coverage.uncovered_areas || []).length > 0 ? `
                    <div class="cum-coverage-section">
                        <span class="cum-cov-label" style="color:#ef4444"><i class="fas fa-xmark"></i> Not Covered</span>
                        <ul>${(coverage.uncovered_areas || []).map(a => `<li>${a}</li>`).join('')}</ul>
                    </div>
                ` : ''}
            </div>
            <div class="glass-card" style="padding:1.5rem;">
                <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                    <i class="fas fa-star" style="color:#eab308"></i> Organizational Strengths
                </h3>
                ${strengths.length > 0 ? `
                    <ul class="cum-strengths">${strengths.map(s => `<li><i class="fas fa-check-circle" style="color:#22c55e;margin-right:.4rem;"></i>${s}</li>`).join('')}</ul>
                ` : '<p style="color:var(--text-muted)">No strengths identified.</p>'}
            </div>
        </div>

        <!-- Top Priorities -->
        ${priorities.length > 0 ? `
        <div class="glass-card" style="padding:1.5rem;">
            <h3 style="margin:0 0 1rem;display:flex;align-items:center;gap:.5rem;">
                <i class="fas fa-flag" style="color:#f97316"></i> Top Priorities
            </h3>
            ${priorities.map((p, i) => `
                <div class="cum-priority">
                    <div class="cum-priority-num">${i + 1}</div>
                    <div class="cum-priority-content">
                        <div style="font-weight:600;">${p.action || 'Action'}</div>
                        <span class="cum-badge" style="background:${severityColor(p.priority)}22;color:${severityColor(p.priority)};text-transform:uppercase;font-size:.7rem;">${p.priority || 'medium'}</span>
                        ${(p.affected_documents || []).length > 0 ? `<div style="font-size:.78rem;color:var(--text-muted);margin-top:.3rem;"><i class="fas fa-file-lines"></i> ${p.affected_documents.join(', ')}</div>` : ''}
                        ${p.rationale ? `<p style="margin:.3rem 0 0;font-size:.85rem;color:var(--text-secondary);">${p.rationale}</p>` : ''}
                    </div>
                </div>
            `).join('')}
        </div>
        ` : ''}
    `;

    // Insert after batch tabs
    const batchTabs = document.getElementById('batchDocTabs');
    batchTabs.insertAdjacentElement('afterend', view);
}

function wireAnalysisTabs() {
    document.querySelectorAll('.analysis-tabs .tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.analysis-tabs .tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`panel-${tab.dataset.tab}`).classList.add('active');
        });
    });
}

function saveDocument(docId) {
    // Show the chunk size selection modal
    const modal = document.getElementById('chunkModal');
    modal.style.display = 'flex';

    // Reset to medium preset
    document.querySelector('input[name="chunkPreset"][value="medium"]').checked = true;
    document.querySelectorAll('.chunk-option').forEach(o => {
        o.classList.toggle('selected', o.dataset.preset === 'medium');
    });

    // Handle radio card selection highlighting
    document.querySelectorAll('.chunk-option input[type="radio"]').forEach(radio => {
        radio.onchange = () => {
            document.querySelectorAll('.chunk-option').forEach(o => o.classList.remove('selected'));
            radio.closest('.chunk-option').classList.add('selected');
        };
    });

    // Cancel button
    document.getElementById('chunkCancelBtn').onclick = () => {
        modal.style.display = 'none';
    };

    // Close on backdrop click
    modal.onclick = (e) => {
        if (e.target === modal) modal.style.display = 'none';
    };

    // Confirm button → save with selected preset
    document.getElementById('chunkConfirmBtn').onclick = () => {
        const preset = document.querySelector('input[name="chunkPreset"]:checked').value;
        modal.style.display = 'none';
        _executeSave(docId, preset);
    };
}

async function _executeSave(docId, chunkPreset) {
    const btn = document.getElementById('saveDocBtn');
    const heroCard = document.querySelector('.score-hero');

    // Show overlay on the score hero card
    let overlay = null;
    if (heroCard) {
        heroCard.style.position = 'relative';
        overlay = document.createElement('div');
        overlay.className = 'delete-overlay save-overlay';
        overlay.innerHTML = `
            <i class="fas fa-spinner fa-spin"></i>
            <span>Saving to Knowledge Base (${chunkPreset} chunks)…</span>
            <div class="delete-progress-bar"><div class="delete-progress-fill save-progress-fill"></div></div>
        `;
        heroCard.appendChild(overlay);
        const fill = overlay.querySelector('.delete-progress-fill');
        fill.style.width = '20%';
        setTimeout(() => { fill.style.width = '50%'; }, 400);
        setTimeout(() => { fill.style.width = '75%'; }, 1000);
    }

    // Disable save button
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…';

    try {
        const res = await fetch(`${API_BASE}/api/documents/${docId}/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chunk_preset: chunkPreset }),
        });
        if (res.ok) {
            const data = await res.json();
            const chunks = data.chunks_indexed || 0;
            if (overlay) {
                const fill = overlay.querySelector('.delete-progress-fill');
                fill.style.width = '100%';
                overlay.querySelector('span').textContent = `Saved! (${chunks} chunks indexed, ${chunkPreset} size)`;
                overlay.querySelector('i').className = 'fas fa-check-circle';
            }
            await new Promise(r => setTimeout(r, 1200));
            if (overlay) overlay.remove();
            btn.innerHTML = '<i class="fas fa-check"></i> Saved';
            btn.disabled = true;
        } else {
            const data = await res.json();
            if (overlay) overlay.remove();
            btn.innerHTML = '<i class="fas fa-bookmark"></i> Save to Knowledge Base';
            btn.disabled = false;
            alert(data.error || 'Save failed');
        }
    } catch (e) {
        console.error('Save error', e);
        if (overlay) overlay.remove();
        btn.innerHTML = '<i class="fas fa-bookmark"></i> Save to Knowledge Base';
        btn.disabled = false;
        alert('Failed to save document');
    }
}

async function saveAllBatchDocuments(batchId) {
    const btn = document.getElementById('saveAllBtn');

    // Show chunk selection modal for batch
    const modal = document.getElementById('chunkModal');
    modal.style.display = 'flex';

    document.querySelector('input[name="chunkPreset"][value="medium"]').checked = true;
    document.querySelectorAll('.chunk-option').forEach(o => {
        o.classList.toggle('selected', o.dataset.preset === 'medium');
    });

    document.querySelectorAll('.chunk-option input[type="radio"]').forEach(radio => {
        radio.onchange = () => {
            document.querySelectorAll('.chunk-option').forEach(o => o.classList.remove('selected'));
            radio.closest('.chunk-option').classList.add('selected');
        };
    });

    document.getElementById('chunkCancelBtn').onclick = () => { modal.style.display = 'none'; };
    modal.onclick = (e) => { if (e.target === modal) modal.style.display = 'none'; };

    document.getElementById('chunkConfirmBtn').onclick = async () => {
        const preset = document.querySelector('input[name="chunkPreset"]:checked').value;
        modal.style.display = 'none';

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving All…';

        try {
            const res = await fetch(`${API_BASE}/api/batch-analysis/${batchId}/save-all`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chunk_preset: preset }),
            });

            if (res.ok) {
                const data = await res.json();
                const count = (data.saved_documents || []).length;
                btn.innerHTML = `<i class="fas fa-check"></i> All ${count} Saved`;
                btn.disabled = true;

                // Reload current analysis to refresh state
                if (currentDocId) {
                    setTimeout(() => loadAnalysis(currentDocId, batchId), 500);
                }
            } else {
                const data = await res.json();
                btn.innerHTML = '<i class="fas fa-bookmark"></i> Save All to Knowledge Base';
                btn.disabled = false;
                alert(data.error || 'Save all failed');
            }
        } catch (e) {
            console.error('Save all error', e);
            btn.innerHTML = '<i class="fas fa-bookmark"></i> Save All to Knowledge Base';
            btn.disabled = false;
            alert('Failed to save batch documents');
        }
    };
}

function renameDocumentFromCard(btn) {
    const card = btn.closest('[data-doc-id]');
    if (!card) return;
    const docId = card.dataset.docId;
    const currentName = card.dataset.docName || '';
    renameDocument(docId, currentName);
}

function deleteDocumentFromCard(btn) {
    const card = btn.closest('[data-doc-id]');
    if (!card) return;
    const docId = card.dataset.docId;
    const name = card.dataset.docName || 'this document';
    deleteDocument(docId, name);
}

async function renameDocument(docId, currentName) {
    const newName = prompt('Enter new name:', currentName || '');
    if (!newName || newName.trim() === currentName) return;
    try {
        const res = await fetch(`${API_BASE}/api/documents/${docId}/rename`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: newName.trim() }),
        });
        if (res.ok) {
            // Refresh whichever page we're on
            if (currentPage === 'documents') loadDocuments();
            if (currentPage === 'chat') loadChatDocs();
        } else {
            const err = await res.json();
            alert('Rename failed: ' + (err.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function deleteDocument(docId, filename) {
    if (!confirm(`Delete "${filename}"?\n\nThis will permanently remove the document and its analysis.`)) return;

    // Show delete overlay on the document card (or full-page overlay on analysis page)
    let overlay = null;
    const card = document.querySelector(`.doc-card[data-doc-id="${docId}"]`) ||
        [...document.querySelectorAll('.doc-card')].find(c => c.onclick?.toString().includes(docId));

    if (card) {
        card.style.position = 'relative';
        card.style.pointerEvents = 'none';
        overlay = document.createElement('div');
        overlay.className = 'delete-overlay';
        overlay.innerHTML = `
            <i class="fas fa-spinner fa-spin"></i>
            <span>Deleting…</span>
            <div class="delete-progress-bar"><div class="delete-progress-fill"></div></div>
        `;
        card.appendChild(overlay);
        // Animate progress
        const fill = overlay.querySelector('.delete-progress-fill');
        fill.style.width = '30%';
        setTimeout(() => { fill.style.width = '60%'; }, 300);
        setTimeout(() => { fill.style.width = '80%'; }, 700);
    }

    try {
        const res = await fetch(`${API_BASE}/api/documents/${docId}`, { method: 'DELETE' });
        if (res.ok) {
            if (overlay) {
                const fill = overlay.querySelector('.delete-progress-fill');
                fill.style.width = '100%';
                overlay.querySelector('span').textContent = 'Deleted!';
                overlay.querySelector('i').className = 'fas fa-check-circle';
            }
            // Brief pause to show completion state
            await new Promise(r => setTimeout(r, 600));
            // Refresh whichever page the user is on
            if (currentPage === 'analysis') {
                navigateTo('documents');
            } else {
                navigateTo(currentPage);
            }
        } else {
            if (overlay) overlay.remove();
            if (card) card.style.pointerEvents = '';
            const data = await res.json();
            alert(data.error || 'Delete failed');
        }
    } catch (e) {
        console.error('Delete error', e);
        if (overlay) overlay.remove();
        if (card) card.style.pointerEvents = '';
        alert('Failed to delete document');
    }
}

// ============================================================
// CHAT  (Unified Knowledge-Base RAG)
// ============================================================
async function loadChatDocs() {
    try {
        // Load KB stats
        const [docsRes, statsRes] = await Promise.all([
            fetch(`${API_BASE}/api/documents`),
            fetch(`${API_BASE}/api/kb/stats`),
        ]);
        const docsData = await docsRes.json();
        const stats = await statsRes.json();
        const docs = (docsData.documents || []).filter(d => d.is_saved);
        const list = document.getElementById('chatDocList');
        const empty = document.getElementById('chatDocEmpty');

        // Update KB stats
        document.getElementById('kbDocCount').textContent = stats.total_documents || 0;
        document.getElementById('kbChunkCount').textContent = stats.total_chunks || 0;

        if (!docs.length) {
            list.innerHTML = '';
            empty.style.display = '';
            return;
        }
        empty.style.display = 'none';
        list.innerHTML = docs.map(d => {
            const chunkInfo = (stats.documents || []).find(sd => sd.doc_id === d.id);
            const chunks = chunkInfo ? chunkInfo.chunks : 0;
            return `
                <div class="chat-doc-item" data-doc-id="${d.id}" data-doc-name="${escAttr(d.original_filename || d.filename)}">
                    <i class="fas fa-file-lines"></i>
                    <div class="chat-doc-info">
                        <span class="chat-doc-name">${escHtml(d.original_filename || d.filename)}</span>
                        <span class="chat-doc-meta">${chunks} chunks indexed</span>
                    </div>
                    <button class="btn-icon-only" onclick="renameDocumentFromCard(this)" title="Rename">
                        <i class="fas fa-pen" style="color:var(--accent-primary);font-size:.7rem"></i>
                    </button>
                    <button class="btn-icon-only" onclick="deleteDocumentFromCard(this)" title="Delete">
                        <i class="fas fa-trash-alt" style="color:var(--accent-danger);font-size:.7rem"></i>
                    </button>
                </div>
            `;
        }).join('');

        // Load global chat history
        loadChatHistory();
    } catch (e) {
        console.error('Chat docs error', e);
    }
}

async function loadChatHistory() {
    const messagesEl = document.getElementById('chatMessages');
    try {
        const res = await fetch(`${API_BASE}/api/chat/history`);
        const data = await res.json();
        const msgs = data.messages || [];

        if (msgs.length) {
            messagesEl.innerHTML = msgs.map(m => chatMsgHtml(m.role, m.message, m.tokens_used)).join('');
        } else {
            messagesEl.innerHTML = `
                <div class="chat-welcome">
                    <i class="fas fa-robot"></i>
                    <h3>Knowledge Base Assistant</h3>
                    <p>Ask questions about any of your saved policy documents. I'll search across all of them and cite my sources.</p>
                </div>
            `;
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;

        // Init session tokens from history
        currentSessionTokens = msgs.reduce((sum, m) => sum + (m.tokens_used || 0), 0);
        updateSessionTokensUI();
    } catch (e) {
        console.error('Chat history error', e);
    }
}

function formatChatMessage(text) {
    // Split text into lines for block-level processing
    const lines = text.split('\n');
    const outputBlocks = [];
    let i = 0;

    while (i < lines.length) {
        // ---- Detect markdown table ----
        // A table starts with a pipe-delimited row, followed by a separator row (|---|---|)
        if (i + 1 < lines.length &&
            lines[i].trim().startsWith('|') &&
            lines[i].trim().endsWith('|') &&
            /^\|[\s\-:|]+\|$/.test(lines[i + 1].trim())) {

            const tableRows = [];
            const headerLine = lines[i];
            tableRows.push(headerLine);
            i++; // skip separator line
            i++; // move past separator

            // Collect data rows
            while (i < lines.length && lines[i].trim().startsWith('|') && lines[i].trim().endsWith('|')) {
                tableRows.push(lines[i]);
                i++;
            }

            // Parse into HTML table
            const parseRow = (line) => line.split('|').slice(1, -1).map(c => c.trim());
            const headers = parseRow(tableRows[0]);
            const dataRows = tableRows.slice(1).map(parseRow);

            let tableHtml = '<div class="chat-table-wrapper"><table class="chat-table"><thead><tr>';
            headers.forEach(h => { tableHtml += `<th>${escHtml(h)}</th>`; });
            tableHtml += '</tr></thead><tbody>';
            dataRows.forEach(row => {
                tableHtml += '<tr>';
                row.forEach(cell => {
                    // Apply inline formatting to cell content
                    let cellHtml = escHtml(cell);
                    // Convert escaped <br> back to actual line breaks
                    cellHtml = cellHtml.replace(/&lt;br&gt;/gi, '<br>');
                    cellHtml = cellHtml.replace(/&lt;br\s*\/&gt;/gi, '<br>');
                    // Turn dash-prefixed items into mini list items
                    cellHtml = cellHtml.replace(/<br>\s*-\s+/g, '<br>• ');
                    if (cellHtml.startsWith('- ')) cellHtml = '• ' + cellHtml.substring(2);
                    // Bold
                    cellHtml = cellHtml.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                    // Citations
                    cellHtml = cellHtml.replace(/\[Source:\s*([^\]]+)\]/g,
                        '<span class="citation-badge"><i class="fas fa-file-lines"></i> $1</span>');
                    tableHtml += `<td>${cellHtml}</td>`;
                });
                tableHtml += '</tr>';
            });
            tableHtml += '</tbody></table></div>';
            outputBlocks.push(tableHtml);
            continue;
        }

        // ---- Detect bullet list (- item or * item) ----
        if (/^\s*[-*]\s+/.test(lines[i])) {
            let listHtml = '<ul class="chat-list">';
            while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
                let itemText = escHtml(lines[i].replace(/^\s*[-*]\s+/, ''));
                itemText = itemText.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                itemText = itemText.replace(/\[Source:\s*([^\]]+)\]/g,
                    '<span class="citation-badge"><i class="fas fa-file-lines"></i> $1</span>');
                listHtml += `<li>${itemText}</li>`;
                i++;
            }
            listHtml += '</ul>';
            outputBlocks.push(listHtml);
            continue;
        }

        // ---- Detect numbered list (1. item) ----
        if (/^\s*\d+[\.\)]\s+/.test(lines[i])) {
            let listHtml = '<ol class="chat-list">';
            while (i < lines.length && /^\s*\d+[\.\)]\s+/.test(lines[i])) {
                let itemText = escHtml(lines[i].replace(/^\s*\d+[\.\)]\s+/, ''));
                itemText = itemText.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                itemText = itemText.replace(/\[Source:\s*([^\]]+)\]/g,
                    '<span class="citation-badge"><i class="fas fa-file-lines"></i> $1</span>');
                listHtml += `<li>${itemText}</li>`;
                i++;
            }
            listHtml += '</ol>';
            outputBlocks.push(listHtml);
            continue;
        }

        // ---- Regular line ----
        let lineHtml = escHtml(lines[i]);
        // Bold
        lineHtml = lineHtml.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        // Citations
        lineHtml = lineHtml.replace(/\[Source:\s*([^\]]+)\]/g,
            '<span class="citation-badge"><i class="fas fa-file-lines"></i> $1</span>');
        outputBlocks.push(lineHtml);
        i++;
    }

    // Join blocks: tables/lists are already block elements, regular lines joined with <br>
    let result = '';
    for (let b = 0; b < outputBlocks.length; b++) {
        const block = outputBlocks[b];
        const isBlockElement = block.startsWith('<div') || block.startsWith('<ul') || block.startsWith('<ol');
        if (isBlockElement) {
            result += block;
        } else {
            result += (b > 0 && !outputBlocks[b - 1].startsWith('<div') && !outputBlocks[b - 1].startsWith('<ul') && !outputBlocks[b - 1].startsWith('<ol') ? '<br>' : '') + block;
        }
    }
    return result;
}

function chatMsgHtml(role, message, tokensUsed = 0, extraHtml = '') {
    const icon = role === 'user' ? 'fa-user' : 'fa-robot';
    const formatted = role === 'assistant' ? formatChatMessage(message) : escHtml(message).replace(/\n/g, '<br>');

    let tokensHtml = '';
    if (role === 'assistant' && tokensUsed > 0) {
        tokensHtml = `<div class="chat-tokens-badge"><i class="fas fa-coins"></i> ${tokensUsed.toLocaleString()} tokens</div>`;
    }

    return `
        <div class="chat-msg ${role}">
            <div class="chat-avatar"><i class="fas ${icon}"></i></div>
            <div class="chat-bubble">
                ${extraHtml}
                ${formatted}
                ${tokensHtml}
            </div>
        </div>
    `;
}

// ---- Chat File Upload (Claude-style) ----
let chatFileSessionId = null;

document.getElementById('chatFileInput').addEventListener('change', handleChatFileUpload);

async function handleChatFileUpload(e) {
    const file = e.target.files[0];
    if (!file) return;

    // Check size client-side
    if (file.size > 20 * 1024 * 1024) {
        alert('File too large. Max 20 MB.');
        e.target.value = '';
        return;
    }

    const pill = document.getElementById('chatFilePill');
    const nameEl = document.getElementById('chatFileName');
    const chunksEl = document.getElementById('chatFileChunks');

    // Show pill in loading state
    nameEl.textContent = file.name;
    chunksEl.textContent = 'Processing…';
    pill.style.display = 'flex';

    const formData = new FormData();
    formData.append('file', file);

    try {
        const res = await fetch(`${API_BASE}/api/chat/upload`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json();

        if (res.ok) {
            chatFileSessionId = data.session_id;
            nameEl.textContent = data.filename;
            chunksEl.textContent = `${data.chunk_count} chunks · ${(data.char_count / 1000).toFixed(1)}k chars`;
            document.getElementById('chatInput').placeholder = `Ask about "${data.filename}" and your knowledge base…`;
        } else {
            alert('Upload failed: ' + (data.error || 'Unknown error'));
            pill.style.display = 'none';
            chatFileSessionId = null;
        }
    } catch (err) {
        alert('Upload error: ' + err.message);
        pill.style.display = 'none';
        chatFileSessionId = null;
    }

    // Reset file input so same file can be re-uploaded
    e.target.value = '';
}

async function removeChatFile() {
    if (chatFileSessionId) {
        try {
            await fetch(`${API_BASE}/api/chat/clear-file`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: chatFileSessionId }),
            });
        } catch (e) { /* ignore */ }
    }
    chatFileSessionId = null;
    document.getElementById('chatFilePill').style.display = 'none';
    document.getElementById('chatInput').placeholder = 'Ask a question across all saved documents…';
}

async function saveChatFileToKB(sessionId) {
    const btn = document.querySelector(`[data-save-session="${sessionId}"]`);
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…';
    }
    try {
        const res = await fetch(`${API_BASE}/api/chat/save-file`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await res.json();
        if (res.ok) {
            if (btn) {
                btn.innerHTML = '<i class="fas fa-check"></i> Saved to Knowledge Base!';
                btn.classList.add('saved');
            }
            // Clear the file session since it's now in KB
            if (chatFileSessionId === sessionId) {
                chatFileSessionId = null;
                document.getElementById('chatFilePill').style.display = 'none';
                document.getElementById('chatInput').placeholder = 'Ask a question across all saved documents…';
            }
            // Refresh KB stats
            loadChatDocs();
        } else {
            alert('Save failed: ' + (data.error || 'Unknown error'));
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-bookmark"></i> Save to Knowledge Base';
            }
        }
    } catch (e) {
        alert('Error: ' + e.message);
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-bookmark"></i> Save to Knowledge Base';
        }
    }
}

async function generateFilledDownload(sessionId, btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating file…';

    try {
        const res = await fetch(`${API_BASE}/api/chat/fill-document`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await res.json();

        if (res.ok && data.download_url) {
            // Replace button with download link
            btn.outerHTML = `
                <a href="${API_BASE}${data.download_url}" class="chat-download-btn" download>
                    <i class="fas fa-download"></i> Download ${escHtml(data.filename)}
                </a>
            `;
            if (data.tokens_used) {
                currentSessionTokens += data.tokens_used;
                updateSessionTokensUI();
            }
        } else {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-file-export"></i> Download as Filled File';
            alert('Error: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-file-export"></i> Download as Filled File';
        alert('Error: ' + e.message);
    }
}

// Send chat
let currentSessionTokens = 0;

function updateSessionTokensUI() {
    const el = document.getElementById('kbSessionTokens');
    if (el) el.textContent = currentSessionTokens.toLocaleString();
}

document.getElementById('chatSendBtn').addEventListener('click', sendChat);

document.getElementById('btnResetTokens')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const icon = btn.querySelector('i');

    if (!confirm('Are you sure you want to reset all token tracking to ZERO?')) return;

    icon.classList.add('fa-spin');
    try {
        const res = await fetch(`${API_BASE}/api/stats/reset`, { method: 'POST' });
        if (res.ok) {
            await loadDashboard(); // refresh stats right away
        } else {
            console.error('Failed to reset tokens');
        }
    } catch (e) {
        console.error('Error resetting tokens', e);
    } finally {
        icon.classList.remove('fa-spin');
    }
});
document.getElementById('chatInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') sendChat();
});

// Clear history
document.getElementById('clearChatBtn').addEventListener('click', async () => {
    if (!confirm('Clear all chat history?')) return;
    try {
        await fetch(`${API_BASE}/api/chat/history`, { method: 'DELETE' });
        const messagesEl = document.getElementById('chatMessages');
        messagesEl.innerHTML = `
            <div class="chat-welcome">
                <i class="fas fa-robot"></i>
                <h3>Knowledge Base Assistant</h3>
                <p>Ask questions about any of your saved policy documents. I'll search across all of them and cite my sources.</p>
            </div>
        `;

        currentSessionTokens = 0;
        updateSessionTokensUI();
    } catch (e) {
        console.error('Clear history error', e);
    }
});

// Reindex KB
document.getElementById('reindexBtn').addEventListener('click', async () => {
    if (!confirm('Re-index all saved documents in the Knowledge Base?\nThis may take a moment for large documents.')) return;

    const btn = document.getElementById('reindexBtn');
    const progress = document.getElementById('reindexProgress');
    const fill = document.getElementById('reindexFill');
    const msgEl = document.getElementById('reindexMessage');
    const pctEl = document.getElementById('reindexPercent');
    const closeBtn = document.getElementById('closeReindexBtn');

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Reindexing…';
    progress.style.display = 'block';
    closeBtn.style.display = 'none';
    fill.style.width = '0%';
    msgEl.textContent = 'Starting reindex…';
    pctEl.textContent = '0%';

    try {
        const res = await fetch(`${API_BASE}/api/kb/reindex`, { method: 'POST' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.error || 'Failed to start reindex');
        }

        // Poll for progress
        const poll = setInterval(async () => {
            try {
                const sr = await fetch(`${API_BASE}/api/kb/reindex/status`);
                const st = await sr.json();
                const pct = st.total > 0 ? Math.round((st.current / st.total) * 100) : 0;

                fill.style.width = pct + '%';
                pctEl.textContent = pct + '%';
                msgEl.textContent = st.message || 'Processing…';

                if (st.status === 'done' || st.status === 'error' || st.status === 'idle') {
                    clearInterval(poll);
                    fill.style.width = '100%';
                    pctEl.textContent = '100%';
                    msgEl.textContent = st.status === 'error' ? 'Error!' : (st.message || 'Done!');

                    if (st.status === 'error') {
                        msgEl.style.color = 'var(--accent-danger)';
                    } else {
                        msgEl.style.color = '';
                    }

                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-sync-alt"></i> Reindex KB';

                    // Refresh KB stats
                    loadKBStats();

                    // Show close button
                    closeBtn.style.display = 'block';
                    closeBtn.onclick = () => {
                        progress.style.display = 'none';
                    };
                }
            } catch (e) {
                clearInterval(poll);
                console.error('Poll error', e);
            }
        }, 1500);

    } catch (e) {
        alert('Reindex failed: ' + e.message);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-sync-alt"></i> Reindex KB';
        progress.style.display = 'none';
    }
});

async function sendChat() {
    const input = document.getElementById('chatInput');
    const message = input.value.trim();
    if (!message) return;

    const messagesEl = document.getElementById('chatMessages');
    // Remove welcome
    const welcome = messagesEl.querySelector('.chat-welcome');
    if (welcome) welcome.remove();

    // Add user message — include file chip if a file is attached
    let fileChipHtml = '';
    if (chatFileSessionId) {
        const pillName = document.getElementById('chatFileName').textContent;
        fileChipHtml = `<div class="chat-msg-file-chip"><i class="fas fa-file-lines"></i> ${escHtml(pillName)}</div>`;
    }
    messagesEl.innerHTML += chatMsgHtml('user', message, 0, fileChipHtml);
    input.value = '';

    // Typing indicator
    const typingId = 'typing-' + Date.now();
    messagesEl.innerHTML += `<div class="chat-msg assistant" id="${typingId}">
        <div class="chat-avatar"><i class="fas fa-robot"></i></div>
        <div class="chat-bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>
    </div>`;
    messagesEl.scrollTop = messagesEl.scrollHeight;

    try {
        const reqBody = { message };
        if (chatFileSessionId) reqBody.session_id = chatFileSessionId;

        const res = await fetch(`${API_BASE}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reqBody),
        });
        const data = await res.json();

        // Remove typing
        document.getElementById(typingId)?.remove();

        if (data.answer) {
            messagesEl.innerHTML += chatMsgHtml('assistant', data.answer, data.tokens_used);

            // Update session tokens
            if (data.tokens_used) {
                currentSessionTokens += data.tokens_used;
                updateSessionTokensUI();
            }

            // Show citations summary if available
            if (data.citations && data.citations.length) {
                const citHtml = data.citations.map(c =>
                    `<span class="citation-badge"><i class="fas fa-file-lines"></i> ${escHtml(typeof c === 'string' ? c : c.filename)}</span>`
                ).join(' ');
                messagesEl.innerHTML += `
                    <div class="chat-citations">
                        <span class="citations-label">Sources referenced:</span>
                        ${citHtml}
                    </div>
                `;
            }

            // Show "Save to KB" + "Download as file" if an uploaded file was used
            if (data.has_uploaded_file && data.session_id) {
                messagesEl.innerHTML += `
                    <div class="chat-save-kb-prompt">
                        <div class="chat-action-row">
                            <button class="btn btn-sm btn-primary chat-save-kb-btn" data-save-session="${data.session_id}"
                                onclick="saveChatFileToKB('${data.session_id}')">
                                <i class="fas fa-bookmark"></i> Save to Knowledge Base
                            </button>
                            <button class="btn btn-sm chat-fill-download-btn" id="fillBtn_${data.session_id.substring(0, 8)}"
                                onclick="generateFilledDownload('${data.session_id}', this)">
                                <i class="fas fa-file-export"></i> Download as Filled File
                            </button>
                        </div>
                    </div>
                `;
            }
        } else if (data.error) {
            messagesEl.innerHTML += chatMsgHtml('assistant', 'Error: ' + data.error);
        } else {
            messagesEl.innerHTML += chatMsgHtml('assistant', 'Sorry, I could not process your request.');
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (e) {
        document.getElementById(typingId)?.remove();
        messagesEl.innerHTML += chatMsgHtml('assistant', 'Error: ' + e.message);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
}

// ============================================================
// UTILITIES
// ============================================================
function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

function escAttr(str) {
    return (str || '').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatBytes(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ---- KB Stats ---------------------------------------------------------------
async function loadKBStats() {
    try {
        const res = await fetch(`${API_BASE}/api/kb/stats`);
        const stats = await res.json();
        const docCount = document.getElementById('kbDocCount');
        const chunkCount = document.getElementById('kbChunkCount');
        if (docCount) docCount.textContent = stats.total_documents || 0;
        if (chunkCount) chunkCount.textContent = stats.total_chunks || 0;
    } catch (e) {
        console.error('Load KB stats error', e);
    }
}

// ---- Init -------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    // Check LLM provider — show selector on every load so user sees current engine
    try {
        const res = await fetch(`${API_BASE}/api/settings/llm-provider`);
        if (res.ok) {
            const data = await res.json();
            window._currentProvider = data.provider;
            showLLMProviderModal(data);
        }
    } catch (e) {
        console.log('Provider check skipped (backend not ready?)');
    }
    navigateTo('dashboard');
});
// ============================================================
// SETTINGS — Authorized Applications
// ============================================================
const settingsModal = document.getElementById('settingsModal');
let currentApiKey = ''; // first active app key (for curl example)
let revealedKeys = {};  // track which app keys are revealed

// Initialize settings events
(function initSettings() {
    document.getElementById('nav-settings').addEventListener('click', openSettings);
    document.getElementById('closeSettingsBtn').addEventListener('click', closeSettings);
    document.getElementById('addAppBtn').addEventListener('click', createApp);
    document.getElementById('newAppNameInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') createApp();
    });

    // Close on backdrop click
    settingsModal.onclick = (e) => {
        if (e.target === settingsModal) closeSettings();
    };
})();

function openSettings(e) {
    if (e) e.preventDefault();
    settingsModal.style.display = 'flex';
    loadApps();
    populateApiDocs();
}

function closeSettings() {
    settingsModal.style.display = 'none';
    revealedKeys = {};
}

async function loadApps() {
    const list = document.getElementById('appsList');
    list.innerHTML = '<div style="text-align:center;padding:1rem;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        const res = await fetch(`${API_BASE}/api/system/apps`);
        const data = await res.json();
        const apps = data.apps || [];

        // Set currentApiKey to first active app for curl example
        const firstActive = apps.find(a => a.is_active);
        currentApiKey = firstActive ? firstActive.api_key : '';
        updateCurlExample();

        if (apps.length === 0) {
            list.innerHTML = '<div class="apps-empty"><i class="fas fa-shield-halved"></i><p>No applications registered yet</p></div>';
            return;
        }

        list.innerHTML = apps.map(app => {
            const isRevealed = revealedKeys[app.id];
            const maskedKey = app.api_key.substring(0, 6) + '••••••••••••••••';
            const displayKey = isRevealed ? app.api_key : maskedKey;
            const lastUsed = app.last_used ? timeAgo(new Date(app.last_used)) : 'Never';
            const statusClass = app.is_active ? 'active' : 'disabled';
            const statusLabel = app.is_active ? 'Active' : 'Disabled';

            return `<div class="app-row ${statusClass}">
                <div class="app-info">
                    <div class="app-name-row">
                        <span class="app-name">${escHtml(app.name)}</span>
                        <span class="app-status-badge ${statusClass}">${statusLabel}</span>
                    </div>
                    <div class="app-key-row">
                        <code class="app-key-display">${displayKey}</code>
                        <button class="btn-icon-xs" onclick="toggleRevealKey(${app.id})" title="${isRevealed ? 'Hide' : 'Reveal'}">
                            <i class="fas fa-${isRevealed ? 'eye-slash' : 'eye'}"></i>
                        </button>
                        <button class="btn-icon-xs" onclick="copyAppKey('${app.api_key}')" title="Copy Key">
                            <i class="fas fa-copy"></i>
                        </button>
                    </div>
                    <div class="app-meta">
                        <span><i class="fas fa-clock"></i> Last used: ${lastUsed}</span>
                        <span><i class="fas fa-calendar"></i> Created: ${new Date(app.created_at).toLocaleDateString()}</span>
                    </div>
                </div>
                <div class="app-actions">
                    <button class="btn-icon-xs ${app.is_active ? '' : 'success'}" onclick="toggleApp(${app.id})" title="${app.is_active ? 'Disable' : 'Enable'}">
                        <i class="fas fa-${app.is_active ? 'pause' : 'play'}"></i>
                    </button>
                    <button class="btn-icon-xs danger" onclick="deleteApp(${app.id}, '${escHtml(app.name)}')" title="Revoke & Delete">
                        <i class="fas fa-trash-alt"></i>
                    </button>
                </div>
            </div>`;
        }).join('');

    } catch (e) {
        console.error('Failed to load apps', e);
        list.innerHTML = '<div class="apps-empty"><i class="fas fa-exclamation-triangle"></i><p>Failed to load applications</p></div>';
    }
}

async function createApp() {
    const input = document.getElementById('newAppNameInput');
    const name = input.value.trim();
    if (!name) { input.focus(); return; }

    const btn = document.getElementById('addAppBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

    try {
        const res = await fetch(`${API_BASE}/api/system/apps`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (res.ok) {
            input.value = '';
            const data = await res.json();
            // Auto-reveal the newly created key
            revealedKeys[data.id] = true;
            await loadApps();
        } else {
            const err = await res.json();
            alert(err.error || 'Failed to create application');
        }
    } catch (e) {
        console.error('Failed to create app', e);
        alert('Failed to create application');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-plus"></i> Add';
    }
}

async function deleteApp(appId, appName) {
    if (!confirm(`Revoke access for "${appName}"?\n\nThis will immediately invalidate the app's API key.`)) return;
    try {
        await fetch(`${API_BASE}/api/system/apps/${appId}`, { method: 'DELETE' });
        delete revealedKeys[appId];
        await loadApps();
    } catch (e) {
        console.error('Failed to delete app', e);
        alert('Failed to delete application');
    }
}

async function toggleApp(appId) {
    try {
        await fetch(`${API_BASE}/api/system/apps/${appId}/toggle`, { method: 'PATCH' });
        await loadApps();
    } catch (e) {
        console.error('Failed to toggle app', e);
    }
}

function toggleRevealKey(appId) {
    revealedKeys[appId] = !revealedKeys[appId];
    loadApps();
}

function copyAppKey(key) {
    navigator.clipboard.writeText(key).then(() => {
        // Brief visual feedback via the btn (handled by reload)
    });
}

function timeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);
    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return date.toLocaleDateString();
}

// ---- API Documentation in Settings ----
const API_ENDPOINTS = [
    { method: 'GET', path: '/api/documents', desc: 'List all documents' },
    { method: 'GET', path: '/api/documents/:id', desc: 'Get a single document' },
    { method: 'POST', path: '/api/upload', desc: 'Upload & analyze a single document' },
    { method: 'POST', path: '/api/upload-batch', desc: 'Upload & analyze multiple documents' },
    { method: 'DELETE', path: '/api/documents/:id', desc: 'Delete a document' },
    { method: 'GET', path: '/api/analysis/:id', desc: 'Get analysis results for a document' },
    { method: 'POST', path: '/api/documents/:id/check-frameworks', desc: 'Run framework comparison' },
    { method: 'GET', path: '/api/batch-analysis/:batchId', desc: 'Get batch analysis results' },
    { method: 'POST', path: '/api/documents/:id/save', desc: 'Save document to knowledge base' },
    { method: 'POST', path: '/api/chat', desc: 'Send a knowledge chat message' },
    { method: 'GET', path: '/api/chat/history', desc: 'Get chat history' },
    { method: 'DELETE', path: '/api/chat/history', desc: 'Clear chat history' },
    { method: 'GET', path: '/api/kb/stats', desc: 'Knowledge base statistics' },
    { method: 'GET', path: '/api/trends', desc: 'Historical score trends' },
    { method: 'GET', path: '/api/frameworks', desc: 'List uploaded framework standards' },
    { method: 'POST', path: '/api/frameworks/upload', desc: 'Upload a framework standard' },
];

function populateApiDocs() {
    const list = document.getElementById('apiEndpointList');
    if (!list) return;
    list.innerHTML = API_ENDPOINTS.map(ep => {
        const methodClass = ep.method.toLowerCase();
        return `<div class="api-ep-row">
            <span class="api-method ${methodClass}">${ep.method}</span>
            <code class="api-ep-path">${ep.path}</code>
            <span class="api-ep-desc">${ep.desc}</span>
        </div>`;
    }).join('');

    updateCurlExample();
}

function updateCurlExample() {
    const el = document.getElementById('apiCurlExample');
    if (!el) return;
    const key = currentApiKey || 'sk-your-key-here';
    el.textContent = `curl -X GET "http://localhost:3001/api/documents" \\\n  -H "X-API-Key: ${key}"`;
}

// Copy curl example
document.getElementById('copyCurlBtn')?.addEventListener('click', () => {
    const el = document.getElementById('apiCurlExample');
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {
        const btn = document.getElementById('copyCurlBtn');
        const original = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-check" style="color:var(--accent-success)"></i> Copied!';
        setTimeout(() => btn.innerHTML = original, 1500);
    });
});


// ============================================================
// FRAMEWORK STANDARDS PAGE
// ============================================================

const FRAMEWORK_META = {
    CIS: { name: 'CIS Controls', icon: 'fa-shield-halved', description: 'Center for Internet Security Controls', color: '#6366f1' },
    GDPR: { name: 'GDPR', icon: 'fa-euro-sign', description: 'General Data Protection Regulation', color: '#8b5cf6' },
    HIPAA: { name: 'HIPAA', icon: 'fa-hospital', description: 'Health Insurance Portability & Accountability', color: '#ec4899' },
    ISO27001: { name: 'ISO 27001', icon: 'fa-certificate', description: 'Information Security Management System', color: '#06b6d4' },
    NIST: { name: 'NIST CSF', icon: 'fa-landmark', description: 'NIST Cybersecurity Framework', color: '#10b981' },
    SOC2: { name: 'SOC 2', icon: 'fa-file-shield', description: 'Service Organization Control 2', color: '#f59e0b' },
};

async function loadStandardsPage() {
    const grid = document.getElementById('standardsGrid');
    grid.innerHTML = '<p style="color:var(--text-muted); text-align:center;">Loading…</p>';

    try {
        const res = await fetch(`${API_BASE}/api/frameworks`);
        const { frameworks } = await res.json();
        renderStandardsCards(frameworks || {});
    } catch (e) {
        console.error('Failed to load frameworks', e);
        grid.innerHTML = '<p style="color:var(--accent-danger);">Failed to load frameworks</p>';
    }
}

function renderStandardsCards(frameworks) {
    const grid = document.getElementById('standardsGrid');

    grid.innerHTML = Object.entries(FRAMEWORK_META).map(([key, meta]) => {
        const versions = frameworks[key] || [];
        const uploaded = versions.length > 0;

        return `
        <div class="std-card glass-card" data-fw="${key}">
            <div class="std-card-header">
                <div class="std-icon" style="background:${meta.color}20; color:${meta.color}">
                    <i class="fas ${meta.icon}"></i>
                </div>
                <div class="std-info">
                    <h3>${meta.name}</h3>
                    <p>${meta.description}</p>
                </div>
                <span class="std-badge ${uploaded ? 'uploaded' : 'not-uploaded'}">
                    <i class="fas ${uploaded ? 'fa-check-circle' : 'fa-circle-xmark'}"></i>
                    ${uploaded ? `${versions.length} version${versions.length > 1 ? 's' : ''}` : 'Not Uploaded'}
                </span>
            </div>

            ${uploaded ? `
            <div class="std-versions">
                ${versions.map(v => `
                    <div class="std-version-row">
                        <div class="std-version-info">
                            <i class="fas fa-file-lines"></i>
                            <span class="std-version-name">${escHtml(v.filename)}</span>
                            <span class="std-version-label">v${escHtml(v.version)}</span>
                        </div>
                        <div class="std-version-meta">
                            <span>${v.chunk_count} chunks</span>
                            <span>${new Date(v.uploaded_at).toLocaleDateString()}</span>
                            <button class="btn-icon-sm danger" onclick="deleteStandard(${v.id}, '${escHtml(v.filename)}')" title="Delete">
                                <i class="fas fa-trash-alt"></i>
                            </button>
                        </div>
                    </div>
                `).join('')}
            </div>` : ''}

            <div class="std-upload-area">
                <div class="std-upload-form" id="upload-form-${key}">
                    <input type="text" class="std-version-input" id="version-${key}" placeholder="Version (e.g. 2022, v8)" />
                    <label class="std-file-label" id="file-label-${key}">
                        <input type="file" id="file-${key}" accept=".pdf,.docx,.doc,.txt,.xlsx,.xls,.csv" onchange="handleFwFileSelect('${key}')" hidden />
                        <i class="fas fa-cloud-arrow-up"></i>
                        <span>Choose file</span>
                    </label>
                    <button class="btn btn-accent btn-sm" id="upload-btn-${key}" onclick="uploadStandard('${key}')" disabled>
                        <i class="fas fa-upload"></i> Upload
                    </button>
                </div>
            </div>
        </div>
        `;
    }).join('');
}

function handleFwFileSelect(key) {
    const input = document.getElementById(`file-${key}`);
    const label = document.getElementById(`file-label-${key}`);
    const uploadBtn = document.getElementById(`upload-btn-${key}`);
    const versionInput = document.getElementById(`version-${key}`);

    if (input.files.length > 0) {
        label.querySelector('span').textContent = input.files[0].name;
        label.classList.add('has-file');
        // Enable upload button only if version is also set
        uploadBtn.disabled = !versionInput.value.trim();

        // Also listen for version input changes
        versionInput.oninput = () => {
            uploadBtn.disabled = !versionInput.value.trim();
        };
    }
}

async function uploadStandard(key) {
    const fileInput = document.getElementById(`file-${key}`);
    const versionInput = document.getElementById(`version-${key}`);
    const btn = document.getElementById(`upload-btn-${key}`);

    if (!fileInput.files.length || !versionInput.value.trim()) return;

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('framework_key', key);
    formData.append('version', versionInput.value.trim());

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Uploading...';

    try {
        const res = await fetch(`${API_BASE}/api/frameworks/upload`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json();

        if (!res.ok) {
            alert(data.error || 'Upload failed');
            return;
        }

        // Success — reload the standards page
        btn.innerHTML = '<i class="fas fa-check"></i> Uploaded!';
        setTimeout(() => loadStandardsPage(), 800);
    } catch (e) {
        console.error('Framework upload failed', e);
        alert('Failed to upload framework standard');
    } finally {
        btn.innerHTML = '<i class="fas fa-upload"></i> Upload';
        btn.disabled = false;
    }
}

async function deleteStandard(id, filename) {
    if (!confirm(`Delete "${filename}"?\n\nThis will remove the standard from the vector store and it will no longer be used in analyses.`)) return;

    try {
        const res = await fetch(`${API_BASE}/api/frameworks/${id}`, { method: 'DELETE' });
        if (!res.ok) {
            const data = await res.json();
            alert(data.error || 'Delete failed');
            return;
        }
        loadStandardsPage();
    } catch (e) {
        console.error('Framework delete failed', e);
        alert('Failed to delete framework standard');
    }
}

// ============================================================
// LLM PROVIDER SELECTOR
// ============================================================
let selectedProvider = null;

function showLLMProviderModal(providerData) {
    const modal = document.getElementById('llmProviderModal');
    modal.style.display = 'flex';
    selectedProvider = null;

    // Mark current provider
    if (providerData?.provider) {
        const card = document.getElementById(providerData.provider === 'ollama' ? 'llmCardOllama' : 'llmCardBedrock');
        if (card) card.classList.add('current');
    }

    // Check Ollama status
    checkOllamaStatus();

    // Wire up card clicks
    document.querySelectorAll('.llm-provider-card').forEach(card => {
        card.onclick = () => selectProvider(card.dataset.provider);
    });

    // Wire up confirm button
    document.getElementById('llmConfirmBtn').onclick = confirmProvider;
}

async function checkOllamaStatus() {
    const statusEl = document.getElementById('llmOllamaStatus');
    try {
        const res = await fetch(`${API_BASE}/api/ollama/status`);
        if (!res.ok) throw new Error('Failed');
        const data = await res.json();

        if (data.available && data.model_ready) {
            statusEl.innerHTML = `
                <i class="fas fa-check-circle" style="color:var(--accent-success)"></i>
                <span>Ready — ${data.configured_model}</span>
            `;
        } else if (data.available) {
            statusEl.innerHTML = `
                <i class="fas fa-exclamation-circle" style="color:var(--accent-warning)"></i>
                <span>Model not downloaded yet</span>
            `;
        } else {
            statusEl.innerHTML = `
                <i class="fas fa-times-circle" style="color:var(--accent-danger)"></i>
                <span>Ollama server not running</span>
            `;
        }
    } catch (e) {
        statusEl.innerHTML = `
            <i class="fas fa-times-circle" style="color:var(--accent-danger)"></i>
            <span>Cannot connect to Ollama</span>
        `;
    }
}

function selectProvider(provider) {
    selectedProvider = provider;
    document.querySelectorAll('.llm-provider-card').forEach(c => c.classList.remove('selected'));
    const card = document.getElementById(provider === 'ollama' ? 'llmCardOllama' : 'llmCardBedrock');
    if (card) card.classList.add('selected');

    const btn = document.getElementById('llmConfirmBtn');
    const text = document.getElementById('llmConfirmText');
    btn.disabled = false;
    text.textContent = provider === 'ollama' ? 'Continue with Ollama' : 'Continue with AWS Bedrock';
}

async function confirmProvider() {
    if (!selectedProvider) return;

    const btn = document.getElementById('llmConfirmBtn');
    const text = document.getElementById('llmConfirmText');
    btn.disabled = true;

    if (selectedProvider === 'ollama') {
        // Check if model needs to be pulled first
        text.textContent = 'Checking model…';
        try {
            const statusRes = await fetch(`${API_BASE}/api/ollama/status`);
            const statusData = await statusRes.json();

            if (!statusData.available) {
                alert('Ollama server is not running. Please start the Ollama container first.');
                btn.disabled = false;
                text.textContent = 'Continue with Ollama';
                return;
            }

            if (!statusData.model_ready) {
                // Need to pull the model — stream progress via SSE
                text.textContent = 'Downloading model…';
                const pullProgress = document.getElementById('llmPullProgress');
                pullProgress.style.display = '';
                const msgEl = document.getElementById('llmPullMessage');
                const fillEl = document.getElementById('llmPullFill');
                fillEl.style.width = '0%';
                fillEl.classList.remove('indeterminate');
                msgEl.textContent = `Downloading ${statusData.configured_model}…`;

                try {
                    const pullRes = await fetch(`${API_BASE}/api/ollama/pull`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ model: statusData.configured_model }),
                    });

                    const reader = pullRes.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });

                        // Process SSE lines
                        const lines = buffer.split('\n');
                        buffer = lines.pop(); // keep incomplete line in buffer
                        for (const line of lines) {
                            if (!line.startsWith('data: ')) continue;
                            try {
                                const evt = JSON.parse(line.slice(6));
                                const pct = evt.percent || 0;
                                const status = evt.status || '';
                                fillEl.style.width = `${pct}%`;

                                // Format human-readable message
                                if (evt.total && evt.completed) {
                                    const dlMB = (evt.completed / 1048576).toFixed(0);
                                    const totMB = (evt.total / 1048576).toFixed(0);
                                    msgEl.textContent = `${status} — ${dlMB}MB / ${totMB}MB (${pct}%)`;
                                } else {
                                    msgEl.textContent = status || 'Downloading…';
                                }

                                if (status === 'done') {
                                    fillEl.style.width = '100%';
                                    msgEl.textContent = 'Model downloaded!';
                                }
                            } catch (_) { }
                        }
                    }
                } catch (e) {
                    alert('Error downloading model: ' + e.message);
                    pullProgress.style.display = 'none';
                    btn.disabled = false;
                    text.textContent = 'Continue with Ollama';
                    return;
                }

                await new Promise(r => setTimeout(r, 800));
                pullProgress.style.display = 'none';
            }
        } catch (e) {
            alert('Error connecting to Ollama: ' + e.message);
            btn.disabled = false;
            text.textContent = 'Continue with Ollama';
            return;
        }
    }

    // Save the provider choice
    text.textContent = 'Saving…';
    try {
        const res = await fetch(`${API_BASE}/api/settings/llm-provider`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: selectedProvider }),
        });
        if (!res.ok) throw new Error('Failed to save');

        window._currentProvider = selectedProvider;
        document.getElementById('llmProviderModal').style.display = 'none';
    } catch (e) {
        alert('Failed to save provider: ' + e.message);
        btn.disabled = false;
        text.textContent = selectedProvider === 'ollama' ? 'Continue with Ollama' : 'Continue with AWS Bedrock';
    }
}

// Add "Switch AI Engine" to settings
(function addProviderToSettings() {
    const settingsEl = document.querySelector('.settings-modal .settings-header');
    if (!settingsEl) return;

    // Add a provider section at the top of the settings modal
    const section = document.createElement('div');
    section.className = 'settings-section';
    section.id = 'llmProviderSection';
    section.innerHTML = `
        <div class="settings-label">
            <i class="fas fa-microchip"></i> AI Engine
        </div>
        <div style="display:flex;align-items:center;gap:1rem;margin-top:.5rem;">
            <span id="settingsCurrentProvider" class="badge badge-medium" style="font-size:.85rem;padding:.3rem .8rem;">
                Loading…
            </span>
            <button class="btn btn-outline btn-sm" id="switchProviderBtn">
                <i class="fas fa-arrows-rotate"></i> Switch Engine
            </button>
        </div>
    `;
    settingsEl.after(section);

    document.getElementById('switchProviderBtn').addEventListener('click', async () => {
        const settingsModal = document.getElementById('settingsModal');
        settingsModal.style.display = 'none';
        try {
            const res = await fetch(`${API_BASE}/api/settings/llm-provider`);
            const data = await res.json();
            showLLMProviderModal(data);
        } catch (e) {
            showLLMProviderModal({});
        }
    });
})();

// Update provider badge when settings opens
const _origOpenSettings = typeof openSettings === 'function' ? openSettings : null;
if (_origOpenSettings) {
    window._openSettingsOrig = _origOpenSettings;
}

function updateProviderBadge() {
    const el = document.getElementById('settingsCurrentProvider');
    if (!el) return;
    const p = window._currentProvider || 'bedrock';
    el.textContent = p === 'ollama' ? '🟢 Ollama (Local)' : '☁️ AWS Bedrock';
    el.className = `badge ${p === 'ollama' ? 'badge-low' : 'badge-medium'}`;
}

// Hook into settings open
const origOpenFn = openSettings;
openSettings = function (e) {
    origOpenFn(e);
    updateProviderBadge();
};
