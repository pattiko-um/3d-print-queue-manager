// ============================================================
// State
// ============================================================
let tickets = [];
let activeTicketId = null;
let stlFiles = [];
let dragPrintId = null;
const expandedPrints = new Set();
const printBoardStatuses = ['todo', 'awaiting_input', 'in_progress', 'printed'];
const printBoardLabels = {
  todo: 'Queued',
  awaiting_input: 'Awaiting Input',
  in_progress: 'Printing',
  printed: 'Complete'
};

// ============================================================
// API
// ============================================================
async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

// ============================================================
// Toast
// ============================================================
function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  setTimeout(() => { el.className = ''; }, 3000);
}

// ============================================================
// Stats
// ============================================================
async function loadStats() {
  try {
    const s = await api('/stats');
    document.getElementById('stat-todo').textContent = s.tickets.todo || 0;
    document.getElementById('stat-inprog').textContent = s.tickets.in_progress || 0;
    document.getElementById('stat-done').textContent = s.tickets.done || 0;
    document.getElementById('stat-time').textContent = formatTime(s.total_time_minutes);
    document.getElementById('stat-filament').textContent = `${(s.total_filament_g/1000).toFixed(2)} kg`;
  } catch(e) {}
}

// ============================================================
// Ticket List
// ============================================================
async function loadTickets() {
  tickets = await api('/tickets');
  renderTicketList();
  loadStats();
}

function renderTicketList() {
  const el = document.getElementById('ticketList');
  if (!tickets.length) {
    el.innerHTML = '<div class="loading" style="padding:16px;text-align:center">No tickets yet</div>';
    return;
  }
  el.innerHTML = tickets.map(t => `
    <div class="ticket-item ${t.id === activeTicketId ? 'active' : ''}" onclick="selectTicket(${t.id})">
      <div class="ticket-item-header">
        <div class="ticket-item-title">${esc(t.title)}</div>
        <div class="${badgeClass(t.status)}">${statusLabel(t.status)}</div>
      </div>
      <div class="ticket-item-meta">
        ${t.requester ? `<span>${esc(t.requester)}</span>` : ''}
        <span>${t.print_count} file${t.print_count !== 1 ? 's' : ''}</span>
      </div>
      <div class="ticket-item-stats">
        <div class="mini-stat">⏱ <span>${formatTime(t.total_time_minutes)}</span></div>
        <div class="mini-stat">🧵 <span>${t.total_filament_g.toFixed(0)}g</span></div>
      </div>
    </div>
  `).join('');
}

// ============================================================
// Ticket Detail
// ============================================================
async function selectTicket(id) {
  activeTicketId = id;
  renderTicketList();
  const ticket = await api(`/tickets/${id}`);
  renderDetail(ticket);
}

function renderDetail(ticket) {
  const pane = document.getElementById('detailPane');
  const printsDone = ticket.prints.filter(p => p.status === 'printed').length;
  const printsTotal = ticket.prints.length;
  const pct = printsTotal > 0 ? Math.round(printsDone / printsTotal * 100) : 0;

  pane.innerHTML = `
    <div class="detail-header">
      <div class="detail-title-area">
        <div class="detail-ticket-id">TICKET #${String(ticket.id).padStart(4,'0')}</div>
        <div class="detail-title">${esc(ticket.title)}</div>
        <div class="detail-meta">
          ${ticket.requester ? `<div class="detail-meta-item">from <strong>${esc(ticket.requester)}</strong></div>` : ''}
          <div class="detail-meta-item">created <strong>${fmtDate(ticket.created_at)}</strong></div>
          <select class="status-select" onchange="updateTicketStatus(${ticket.id}, this.value)">
            <option value="todo" ${ticket.status==='todo'?'selected':''}>To Do</option>
            <option value="in_progress" ${ticket.status==='in_progress'?'selected':''}>In Progress</option>
            <option value="done" ${ticket.status==='done'?'selected':''}>Done</option>
          </select>
        </div>
      </div>
      <div class="detail-actions">
        <button class="btn btn-sm" onclick="openEditTicketModal(${ticket.id})">Edit</button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="deleteTicket(${ticket.id})">Delete</button>
      </div>
    </div>
    <div class="detail-body">
      ${ticket.notes ? `<div class="notes-block">${esc(ticket.notes)}</div>` : ''}

      <div class="summary-bar">
        <div class="summary-cell">
          <div class="summary-val">${printsTotal}</div>
          <div class="summary-label">Files</div>
        </div>
        <div class="summary-cell">
          <div class="summary-val">${formatTime(ticket.total_time_minutes)}</div>
          <div class="summary-label">Est. Print Time</div>
        </div>
        <div class="summary-cell">
          <div class="summary-val">${ticket.total_filament_g.toFixed(1)}g</div>
          <div class="summary-label">Est. Filament</div>
        </div>
        <div class="summary-cell">
          <div class="summary-val">${pct}%</div>
          <div class="summary-label">Printed</div>
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
        </div>
      </div>

      <div class="section-head">
        <div class="section-label">Print Files (${printsTotal})</div>
        <button class="btn btn-sm" onclick="openAddPrintsModal(${ticket.id})">+ Add Files</button>
      </div>

      <div class="board">
        ${renderBoardColumns(ticket.prints)}
      </div>
    </div>
  `;
}

function renderBoardColumns(prints) {
  const groups = printBoardStatuses.reduce((acc, status) => {
    acc[status] = [];
    return acc;
  }, {});

  prints.forEach(p => {
    const key = printBoardStatuses.includes(p.status) ? p.status : 'todo';
    groups[key].push(p);
  });

  return printBoardStatuses.map(status => renderBoardColumn(status, groups[status])).join('');
}

function renderBoardColumn(status, prints) {
  return `
    <div class="board-column">
      <div class="board-column-header">
        <div class="board-column-title">${esc(printBoardLabels[status])}</div>
        <div class="board-column-count">${prints.length}</div>
      </div>
      <div class="board-column-body"
           id="board-column-${status}"
           ondragover="allowDrop(event)"
           ondragenter="onDragEnter(event)"
           ondragleave="onDragLeave(event)"
           ondrop="dropCard(event,'${status}')">
        ${prints.length === 0
          ? `<div class="loading" style="padding:20px;text-align:center;color:var(--text3)">No prints</div>`
          : prints.map(renderBoardCard).join('')}
      </div>
    </div>
  `;
}

function renderBoardCard(p) {
  const hasError = !!p.parse_error;
  const hasIssues = p.issues && p.issues.length > 0;
  const issueText = hasError ? p.parse_error : hasIssues ? p.issues.join(', ') : '';
  const timeLabel = p.time_formatted || formatTime(p.time_minutes);
  const filamentLabel = p.filament_mass_g != null ? `${p.filament_mass_g.toFixed(0)}g` : '—';
  const dimensionLabel = p.size_x_mm && p.size_y_mm && p.size_z_mm
    ? `${p.size_x_mm}×${p.size_y_mm}×${p.size_z_mm} mm`
    : 'Dimensions unknown';
  const expanded = expandedPrints.has(p.id) ? 'expanded' : '';

  return `
    <div class="board-card ${expanded}" id="board-card-${p.id}"
         draggable="true"
         ondragstart="onDragStart(event, ${p.id})">
      <div class="board-card-summary">
        <div>
          <div class="board-card-title">${esc(p.filename)}</div>
          <div class="board-card-meta">
            <span>⏱ ${timeLabel}</span>
            <span>🧵 ${filamentLabel}</span>
          </div>
          <div class="board-card-meta">
            <span>${esc(dimensionLabel)}</span>
          </div>
        </div>
        <button class="board-card-toggle" onclick="toggleCardDetails(event, ${p.id})">${expanded ? '−' : '+'}</button>
      </div>
      ${issueText ? `<div class="${hasError ? 'board-card-error' : 'board-card-issue'}">⚠ ${esc(issueText)}</div>` : ''}
      <div class="board-card-details">
        <div class="board-card-row">
          <div class="board-card-field">
            <span class="board-card-field-label">Dimensions</span>
            <span class="board-card-field-value">${esc(dimensionLabel)}</span>
          </div>
          <div class="board-card-field">
            <span class="board-card-field-label">Volume</span>
            <span class="board-card-field-value">${p.volume_mm3 != null ? p.volume_mm3.toFixed(0) + ' mm³' : '—'}</span>
          </div>
        </div>
        <div class="board-card-row">
          <div class="board-card-field">
            <span class="board-card-field-label">Print Time</span>
            <span class="board-card-field-value">${timeLabel} ${p.time_minutes != null ? `(${p.time_minutes.toFixed(0)} min)` : ''}</span>
          </div>
          <div class="board-card-field">
            <span class="board-card-field-label">Filament</span>
            <span class="board-card-field-value">${filamentLabel} / ${p.filament_length_m != null ? `${p.filament_length_m}m` : '—'}</span>
          </div>
        </div>
        <div class="board-card-row">
          <div class="board-card-field">
            <span class="board-card-field-label">Layers</span>
            <span class="board-card-field-value">${p.layer_count != null ? p.layer_count.toLocaleString() : '—'}</span>
          </div>
          <div class="board-card-field">
            <span class="board-card-field-label">Triangles</span>
            <span class="board-card-field-value">${p.triangle_count != null ? p.triangle_count.toLocaleString() : '—'}</span>
          </div>
        </div>
        <div class="board-card-actions">
          <select class="status-select" onchange="updatePrintStatus(${p.id}, this.value)">
            <option value="todo" ${p.status==='todo'?'selected':''}>Queued</option>
            <option value="awaiting_input" ${p.status==='awaiting_input'?'selected':''}>Awaiting Input</option>
            <option value="in_progress" ${p.status==='in_progress'?'selected':''}>Printing</option>
            <option value="printed" ${p.status==='printed'?'selected':''}>Complete</option>
          </select>
          <button class="btn btn-sm btn-ghost" onclick="reanalyzePrint(${p.id})" title="Re-run analysis">↺ Re-scan</button>
          <button class="btn btn-sm btn-ghost btn-danger" onclick="deletePrint(${p.id})">Remove</button>
        </div>
      </div>
    </div>
  `;
}

function toggleCardDetails(event, printId) {
  event.stopPropagation();
  if (expandedPrints.has(printId)) {
    expandedPrints.delete(printId);
  } else {
    expandedPrints.add(printId);
  }
  const ticket = activeTicketId ? api(`/tickets/${activeTicketId}`) : null;
  if (ticket) {
    ticket.then(renderDetail);
  }
}

function onDragStart(evt, printId) {
  dragPrintId = printId;
  evt.dataTransfer.setData('text/plain', String(printId));
  evt.dataTransfer.effectAllowed = 'move';
  const el = document.getElementById(`board-card-${printId}`);
  if (el) el.classList.add('dragging');
}

function allowDrop(evt) {
  evt.preventDefault();
}

function onDragEnter(evt) {
  evt.preventDefault();
  evt.currentTarget.classList.add('dragover');
}

function onDragLeave(evt) {
  evt.currentTarget.classList.remove('dragover');
}

async function dropCard(evt, status) {
  evt.preventDefault();
  evt.currentTarget.classList.remove('dragover');
  const payload = evt.dataTransfer.getData('text/plain');
  const printId = dragPrintId || Number(payload);
  if (!printId) return;
  const card = document.getElementById(`board-card-${printId}`);
  if (card) card.classList.remove('dragging');
  dragPrintId = null;
  await updatePrintStatus(Number(printId), status);
}

// ============================================================
// Actions
// ============================================================
async function updateTicketStatus(id, status) {
  await api(`/tickets/${id}`, { method: 'PATCH', body: { status } });
  await loadTickets();
  if (activeTicketId === id) {
    const t = await api(`/tickets/${id}`);
    renderDetail(t);
  }
  toast(`Ticket marked ${statusLabel(status)}`);
}

async function updatePrintStatus(printId, status) {
  await api(`/prints/${printId}`, { method: 'PATCH', body: { status } });
  const t = await api(`/tickets/${activeTicketId}`);
  renderDetail(t);
  await loadStats();
  toast(`Print marked ${printStatusLabel(status)}`);
}

async function deleteTicket(id) {
  if (!confirm('Delete this ticket and all its prints?')) return;
  await api(`/tickets/${id}`, { method: 'DELETE' });
  activeTicketId = null;
  document.getElementById('detailPane').innerHTML = `
    <div class="detail-empty">
      <div class="detail-empty-icon">⬡</div>
      <p>Select a ticket to view details</p>
    </div>`;
  await loadTickets();
  toast('Ticket deleted');
}

async function deletePrint(id) {
  if (!confirm('Remove this file from the ticket?')) return;
  const ticket = await api(`/prints/${id}`, { method: 'DELETE' });
  renderDetail(ticket);
  await loadTickets();
  toast('File removed');
}

async function reanalyzePrint(id) {
  toast('Re-scanning STL…');
  const ticket = await api(`/prints/${id}/reanalyze`, { method: 'POST' });
  renderDetail(ticket);
  toast('Re-scan complete');
}

// ============================================================
// Modals
// ============================================================
function closeModal() {
  document.getElementById('modalContainer').innerHTML = '';
}

function showModal(html) {
  const c = document.getElementById('modalContainer');
  c.innerHTML = `<div class="modal-overlay" onclick="if(event.target===this)closeModal()">${html}</div>`;
}

function openNewTicketModal() {
  showModal(`
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">New Ticket</div>
        <button class="btn btn-ghost btn-sm" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Title *</label>
          <input class="form-input" id="nt-title" placeholder="e.g. Robotics Club — Arm Parts" autofocus>
        </div>
        <div class="form-group">
          <label class="form-label">Requester</label>
          <input class="form-input" id="nt-req" placeholder="Name or team">
        </div>
        <div class="form-group">
          <label class="form-label">Notes</label>
          <textarea class="form-textarea" id="nt-notes" placeholder="Any special instructions…"></textarea>
        </div>
        <div class="form-footer">
          <button class="btn" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="submitNewTicket()">Create Ticket</button>
        </div>
      </div>
    </div>
  `);
  document.getElementById('nt-title').focus();
}

async function submitNewTicket() {
  const title = document.getElementById('nt-title').value.trim();
  if (!title) { toast('Title is required', 'error'); return; }
  const ticket = await api('/tickets', {
    method: 'POST',
    body: {
      title,
      requester: document.getElementById('nt-req').value.trim(),
      notes: document.getElementById('nt-notes').value.trim(),
    }
  });
  closeModal();
  await loadTickets();
  selectTicket(ticket.id);
  toast('Ticket created');
}

async function openEditTicketModal(id) {
  const t = await api(`/tickets/${id}`);
  showModal(`
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">Edit Ticket #${String(id).padStart(4,'0')}</div>
        <button class="btn btn-ghost btn-sm" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Title *</label>
          <input class="form-input" id="et-title" value="${esc(t.title)}">
        </div>
        <div class="form-group">
          <label class="form-label">Requester</label>
          <input class="form-input" id="et-req" value="${esc(t.requester || '')}">
        </div>
        <div class="form-group">
          <label class="form-label">Notes</label>
          <textarea class="form-textarea" id="et-notes">${esc(t.notes || '')}</textarea>
        </div>
        <div class="form-footer">
          <button class="btn" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="submitEditTicket(${id})">Save Changes</button>
        </div>
      </div>
    </div>
  `);
}

async function submitEditTicket(id) {
  const title = document.getElementById('et-title').value.trim();
  if (!title) { toast('Title is required', 'error'); return; }
  const ticket = await api(`/tickets/${id}`, {
    method: 'PATCH',
    body: {
      title,
      requester: document.getElementById('et-req').value.trim(),
      notes: document.getElementById('et-notes').value.trim(),
    }
  });
  closeModal();
  await loadTickets();
  renderDetail(ticket);
  toast('Ticket updated');
}

async function openAddPrintsModal(ticketId) {
  stlFiles = await api('/stl-files');

  const ticket = await api(`/tickets/${ticketId}`);
  const attached = new Set(ticket.prints.map(p => p.filename));
  const available = stlFiles.filter(f => !attached.has(f.filename));
  let selected = new Set();

  function fileRowHtml(f) {
    const sel = selected.has(f.filename);
    return `
      <div class="stl-file-row ${sel ? 'selected' : ''}" onclick="toggleStlFile('${f.filename}')">
        <div class="stl-file-check">${sel ? '✓' : ''}</div>
        <div class="stl-file-name">${esc(f.filename)}</div>
        <div class="stl-file-size">${fmtBytes(f.size_bytes)}</div>
      </div>
    `;
  }

  function renderPicker() {
    const el = document.getElementById('stl-picker');
    if (!available.length) {
      el.innerHTML = `<div class="stl-picker-empty">No new STL files found in stl_files/<br><span style="font-size:10px;color:var(--text3)">Drop .stl files into the stl_files/ directory, then refresh</span></div>`;
      return;
    }
    el.innerHTML = available.map(fileRowHtml).join('');
  }

  window.toggleStlFile = function(name) {
    if (selected.has(name)) selected.delete(name);
    else selected.add(name);
    renderPicker();
  };

  showModal(`
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">Add STL Files</div>
        <button class="btn btn-ghost btn-sm" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <p style="font-size:12px;color:var(--text3);margin-bottom:12px">
          Showing files in <code style="background:var(--surface3);padding:2px 6px;border-radius:3px;font-size:11px">stl_files/</code> not yet attached to this ticket.
        </p>
        <div class="stl-picker" id="stl-picker"></div>
        <div class="form-footer">
          <button class="btn" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="submitAddPrints(${ticketId})">Add Selected</button>
        </div>
      </div>
    </div>
  `);

  renderPicker();

  window.submitAddPrints = async function(tid) {
    if (!selected.size) { toast('Select at least one file', 'error'); return; }
    toast(`Adding ${selected.size} file(s)… analyzing STLs`);
    closeModal();
    for (const filename of selected) {
      await api(`/tickets/${tid}/prints`, { method: 'POST', body: { filename } });
    }
    const ticket = await api(`/tickets/${tid}`);
    renderDetail(ticket);
    await loadTickets();
    toast(`Added ${selected.size} file(s)`);
  };
}

// ============================================================
// Helpers
// ============================================================
function badgeClass(status) {
  const map = {
    'todo': 'badge badge-todo',
    'in_progress': 'badge badge-in_progress',
    'done': 'badge badge-done',
    'print-todo': 'badge badge-todo',
    'print-in_progress': 'badge badge-in_progress',
    'print-printed': 'badge badge-printed',
    'print-awaiting_input': 'badge badge-awaiting_input',
  };
  return map[status] || 'badge badge-todo';
}

function statusLabel(s) {
  return { todo: 'To Do', in_progress: 'In Progress', done: 'Done' }[s] || s;
}

function printStatusLabel(s) {
  return {
    todo: 'Queued',
    awaiting_input: 'Awaiting Input',
    in_progress: 'Printing',
    printed: 'Complete'
  }[s] || s;
}

function formatTime(mins) {
  if (!mins) return '—';
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024*1024) return (b/1024).toFixed(0) + ' KB';
  return (b/1024/1024).toFixed(1) + ' MB';
}

function esc(str) {
  return String(str || '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ============================================================
// Import from directory
// ============================================================
async function rescanDirectory() {
  const btn = document.getElementById('rescanBtn');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '⏳ Scanning…';
  
  showModal(`
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">Importing from Directory</div>
      </div>
      <div class="modal-body">
        <div id="importProgress">Scanning directory...</div>
        <div id="importFiles" style="margin-top: 16px;"></div>
      </div>
    </div>
  `);
  
  try {
    const res = await api('/import-from-directory', { method: 'POST', body: {} });
    
    const filesDiv = document.getElementById('importFiles');
    if (res.files_processed && res.files_processed.length > 0) {
      filesDiv.innerHTML = '<div style="font-size: 12px; color: var(--text3); margin-bottom: 8px;">Files processed:</div>' +
        res.files_processed.map((f, i) => {
          const statusIcon = f.status === 'completed' ? '✅' : f.status === 'error' ? '❌' : f.status === 'skipped' ? '⏭️' : '⏳';
          return `<div style="font-size: 11px; margin-bottom: 4px;">${i+1}. ${statusIcon} ${f.filename} (${f.status})</div>`;
        }).join('');
    }
    
    document.getElementById('importProgress').innerHTML = 'Import complete!';
    
    if (res.created_tickets || res.updated_tickets || res.added_prints) {
      const msg = [
        res.created_tickets > 0 && `${res.created_tickets} new ticket(s)`,
        res.updated_tickets > 0 && `${res.updated_tickets} updated`,
        res.added_prints > 0 && `${res.added_prints} print(s) added`,
      ].filter(Boolean).join(', ');
      
      toast(`✓ Import complete: ${msg}`, 'success');
      await loadTickets();
    } else {
      toast('No new tickets or prints found', 'info');
    }
    
    if (res.errors && res.errors.length > 0) {
      toast(`⚠ ${res.errors.length} error(s) during import`, 'warning');
      console.log('Import errors:', res.errors);
    }
  } catch (err) {
    document.getElementById('importProgress').innerHTML = 'Import failed!';
    toast('Failed to scan directory: ' + err.message, 'error');
    console.error(err);
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

// ============================================================
// Keyboard shortcuts
// ============================================================
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
  if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
    e.preventDefault();
    openNewTicketModal();
  }
});

// ============================================================
// Init
// ============================================================
loadTickets();
