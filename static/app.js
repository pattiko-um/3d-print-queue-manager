// ============================================================
// State
// ============================================================
let tickets = [];
let activeTicketId = null;
let stlFiles = [];
let dragPrintId = null;
let dragTicketId = null;
let showingDeliveredTickets = false;
const expandedPrints = new Set();
const ticketBoardStatuses = ['received', 'awaiting_input', 'queued', 'in_process', 'complete'];
const ticketBoardLabels = {
  received: 'Received',
  awaiting_input: 'Awaiting Input',
  queued: 'Queued',
  in_process: 'Printing',
  complete: 'Complete'
};
const printBoardStatuses = ['to_do', 'awaiting_input', 'queued', 'printing', 'complete'];
const printBoardLabels = {
  to_do: 'To Do',
  awaiting_input: 'Awaiting Input',
  queued: 'Queued',
  printing: 'Printing',
  complete: 'Complete'
};
const expandedTicketCards = new Set();

function ticketUrl(ticket) {
  const id = ticket.external_ticket_id || ticket.id;
  return ticket.ticket_url || `https://teamdynamix.umich.edu/TDNext/Apps/46/Tickets/TicketDet.aspx?TicketID=${id}`;
}

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
    document.getElementById('stat-todo').textContent = s.tickets.received || s.tickets.todo || 0;
    document.getElementById('stat-inprog').textContent = s.tickets.in_process || s.tickets.in_progress || 0;
    document.getElementById('stat-done').textContent = s.tickets.complete || s.tickets.done || 0;
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

  if (showingDeliveredTickets) {
    const delivered = tickets.filter(t => (t.status || '') === 'delivered');
    el.innerHTML = `
      <div class="section-head">
        <div class="section-label">Delivered Tickets</div>
        <button class="btn btn-sm" onclick="showTicketBoard()">Back to board</button>
      </div>
      <div class="ticket-board-delivered">
        ${delivered.length === 0 ? `<div class="loading" style="padding:20px;text-align:center;color:var(--text3)">No delivered tickets</div>` : delivered.map(renderDeliveredTicketCard).join('')}
      </div>
    `;
    return;
  }

  const groups = ticketBoardStatuses.reduce((acc, status) => {
    acc[status] = tickets.filter(t => (t.status || 'received') === status);
    return acc;
  }, {});

  el.innerHTML = `
    <div class="ticket-board">
      ${ticketBoardStatuses.map(status => renderTicketBoardColumn(status, groups[status])).join('')}
    </div>
  `;
}

function showDeliveredTickets() {
  showingDeliveredTickets = true;
  renderTicketList();
}

function showTicketBoard() {
  showingDeliveredTickets = false;
  renderTicketList();
}

function renderDeliveredTicketCard(t) {
  const issueLabel = t.issues && t.issues.length ? `<span class="board-card-issue">⚠ ${esc(t.issues.join(', '))}</span>` : '';
  return `
    <div class="board-card ticket-card delivered-card">
      <div class="board-card-summary">
        <div>
          <div class="board-card-title"><a href="${esc(ticketUrl(t))}" target="_blank" class="ticket-card-title">${esc(t.title)}</a></div>
          <div class="board-card-meta">
            <span style="font-size: 10px; color: var(--text3);">${fmtDate(t.created_at)}</span>
          </div>
          <div class="board-card-meta">
            <span>${t.requester ? esc(t.requester) : 'No requester'}</span>
            <span>${t.remaining_prints} / ${t.print_count} prints remaining</span>
          </div>
          <div class="board-card-meta">
            <span class="board-card-pill ${badgeClass(t.status)}">${esc(statusLabel(t.status))}</span>
          </div>
        </div>
        <button class="board-card-toggle" onclick="selectTicket(${t.id})">›</button>
      </div>
      ${issueLabel}
    </div>
  `;
}

function renderTicketBoardColumn(status, items) {
  const sorted = items.sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  return `
    <div class="board-column">
      <div class="board-column-header">
        <div class="board-column-title">${esc(ticketBoardLabels[status])}</div>
        <div class="board-column-count">${sorted.length}</div>
      </div>
      <div class="board-column-body"
           id="ticket-board-column-${status}"
           ondragover="allowDrop(event)"
           ondragenter="onDragEnter(event)"
           ondragleave="onDragLeave(event)"
           ondrop="dropTicketCard(event,'${status}')">
        ${sorted.length === 0 ? `<div class="loading" style="padding:20px;text-align:center;color:var(--text3)">No tickets</div>` : sorted.map(renderTicketCard).join('')}
      </div>
    </div>
  `;
}

function renderTicketCard(t) {
  const isActive = t.id === activeTicketId ? 'active' : '';
  const isExpandedCard = expandedTicketCards.has(t.id) ? 'expanded' : '';
  const hasIssues = t.issues && t.issues.length > 0;
  const issueLabel = hasIssues ? `<span class="board-card-issue">⚠ ${esc(t.issues.join(', '))}</span>` : '';
  const printsDone = t.prints ? t.prints.filter(p => p.status === 'complete').length : 0;
  const printsTotal = t.print_count || 0;
  const pct = printsTotal > 0 ? Math.round(printsDone / printsTotal * 100) : 0;
  return `
    <div class="board-card ticket-card ${isActive} ${isExpandedCard}"
         id="ticket-board-card-${t.id}"
         draggable="true"
         ondragstart="onTicketDragStart(event, ${t.id})">
      <div class="board-card-header">
        <div class="board-card-title"><span onclick="selectTicket(${t.id})" class="ticket-card-title">${esc(t.title)}</span></div>
      </div>
      <div class="board-card-summary">
        <div class="board-card-meta">
          <span>${t.requester ? esc(t.requester) : 'No requester'}</span> | 
          <span style="font-size: 10px; color: var(--text3);">${fmtDate(t.created_at)}</span> | 
          <span onclick="toggleTicketDetails(event, ${t.id})" class="board-card-link">${expandedTicketCards.has(t.id) ? 'Collapse' : 'Expand'}</span>
        </div>
        <div class="board-card-meta">
        </div>
        <div class="board-card-meta">
          <span>⏱ ${formatTime(t.remaining_time_minutes)}</span>
          <span>🧵 ${t.remaining_filament_g.toFixed(0)}g</span>
        </div>
        <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
      </div>
      ${issueLabel}
      <div class="board-card-details">
        ${t.prints && t.prints.length ? t.prints.map(p => `
          <div class="board-card-row ticket-print-row">
            <span class="ticket-print-name">${esc(p.filename)}</span>
            <span class="board-card-pill ${badgeClass('print-'+p.status)}">${esc(printStatusLabel(p.status))}</span>
          </div>
        `).join('') : '<div class="ticket-print-empty">No prints yet</div>'}
      </div>
    </div>
  `;
}

// ============================================================
// Ticket Detail
// ============================================================
async function selectTicket(id) {
  activeTicketId = id;
  renderTicketList();
  const ticket = await api(`/tickets/${id}`);
  renderDetail(ticket);
  document.querySelector('.app').classList.add('detail-open');
}

function toggleTicketDetails(event, ticketId) {
  event.stopPropagation();
  if (expandedTicketCards.has(ticketId)) {
    expandedTicketCards.delete(ticketId);
  } else {
    expandedTicketCards.add(ticketId);
  }
  renderTicketList();
}

function renderDetail(ticket) {
  const pane = document.getElementById('detailPane');
  const printsDone = ticket.prints.filter(p => p.status === 'complete').length;
  const printsTotal = ticket.prints.length;
  const pct = printsTotal > 0 ? Math.round(printsDone / printsTotal * 100) : 0;

  pane.innerHTML = `
    <div class="detail-header">
      <div class="detail-header-top">
        <button class="btn btn-sm btn-ghost btn-back" onclick="closeTicketDetail()">← Back</button>
      </div>
      <div class="detail-title-area">
        <div class="detail-ticket-id">TICKET #${String(ticket.id).padStart(4,'0')}</div>
        <div class="detail-title">${esc(ticket.title)}</div>
        <div class="detail-meta">
          ${ticket.requester ? `<div class="detail-meta-item">from <strong>${esc(ticket.requester)}</strong></div>` : ''}
          <div class="detail-meta-item">created <strong>${fmtDate(ticket.created_at)}</strong></div>
          ${ticket.status === 'delivered'
            ? `<div class="detail-meta-item"><strong>Delivered</strong></div>`
            : `<select class="status-select" onchange="updateTicketStatus(${ticket.id}, this.value)">
                 <option value="received" ${ticket.status==='received'?'selected':''}>Received</option>
                 <option value="awaiting_input" ${ticket.status==='awaiting_input'?'selected':''}>Awaiting Input</option>
                 <option value="queued" ${ticket.status==='queued'?'selected':''}>Queued</option>
                 <option value="in_process" ${ticket.status==='in_process'?'selected':''}>Printing</option>
                 <option value="complete" ${ticket.status==='complete'?'selected':''}>Complete</option>
               </select>`}
        </div>
      </div>
      <div class="detail-actions">
        <button class="btn btn-sm" onclick="openEditTicketModal(${ticket.id})">Edit</button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="deleteTicket(${ticket.id})">Delete</button>
        ${ticket.status !== 'delivered' ? `<button class="btn btn-sm btn-primary" onclick="deliverTicket(${ticket.id})">Deliver</button>` : ''}
      </div>
    </div>
    <div class="detail-body">
      ${ticket.notes ? `<div class="notes-block">${esc(ticket.notes)}</div>` : ''}

      <div class="summary-bar">
        <div class="summary-cell">
          <div class="summary-val">${printsDone} / ${printsTotal}</div>
          <div class="summary-label">Printed</div>
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
        </div>
        <div class="summary-cell">
          <div class="summary-val">${formatTime(ticket.remaining_time_minutes)}</div>
          <div class="summary-label">Remaining Print Time</div>
        </div>
        <div class="summary-cell">
          <div class="summary-val">${ticket.remaining_filament_g.toFixed(1)}g</div>
          <div class="summary-label">Remaining Filament</div>
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
    const key = printBoardStatuses.includes(p.status) ? p.status : 'to_do';
    groups[key].push(p);
  });

  // Sort prints within each status by updated_at (newest first)
  Object.keys(groups).forEach(status => {
    groups[status].sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
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
            <option value="to_do" ${p.status==='to_do'?'selected':''}>To Do</option>
            <option value="awaiting_input" ${p.status==='awaiting_input'?'selected':''}>Awaiting Input</option>
            <option value="queued" ${p.status==='queued'?'selected':''}>Queued</option>
            <option value="printing" ${p.status==='printing'?'selected':''}>Printing</option>
            <option value="complete" ${p.status==='complete'?'selected':''}>Complete</option>
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

function onTicketDragStart(evt, ticketId) {
  dragTicketId = ticketId;
  evt.dataTransfer.setData('text/plain', String(ticketId));
  evt.dataTransfer.effectAllowed = 'move';
  const el = document.getElementById(`ticket-board-card-${ticketId}`);
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

async function dropTicketCard(evt, status) {
  evt.preventDefault();
  evt.currentTarget.classList.remove('dragover');
  const payload = evt.dataTransfer.getData('text/plain');
  const ticketId = dragTicketId || Number(payload);
  if (!ticketId) return;
  const card = document.getElementById(`ticket-board-card-${ticketId}`);
  if (card) card.classList.remove('dragging');
  dragTicketId = null;
  await updateTicketStatus(Number(ticketId), status);
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

function closeTicketDetail() {
  activeTicketId = null;
  document.querySelector('.app').classList.remove('detail-open');
  renderTicketList();
  document.getElementById('detailPane').innerHTML = `
    <div class="detail-empty">
      <div class="detail-empty-icon">⬡</div>
      <p>Select a ticket to view details</p>
    </div>`;
}

async function updatePrintStatus(printId, status) {
  await api(`/prints/${printId}`, { method: 'PATCH', body: { status } });
  let t = await api(`/tickets/${activeTicketId}`);
  
  // Auto-complete ticket if all prints are complete
  const allComplete = t.prints.every(p => p.status === 'complete');
  if (allComplete && t.status !== 'complete') {
    await api(`/tickets/${activeTicketId}`, { method: 'PATCH', body: { status: 'complete' } });
    t = await api(`/tickets/${activeTicketId}`);
    toast('All prints complete! Ticket marked complete.');
  } else {
    toast(`Print marked ${printStatusLabel(status)}`);
  }
  
  renderDetail(t);
  await loadStats();
  await loadTickets();
}

async function deleteTicket(id) {
  if (!confirm('Delete this ticket and all its prints?')) return;
  await api(`/tickets/${id}`, { method: 'DELETE' });
  activeTicketId = null;
  document.querySelector('.app').classList.remove('detail-open');
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
    'received': 'badge badge-received',
    'awaiting_input': 'badge badge-awaiting_input',
    'queued': 'badge badge-queued',
    'in_process': 'badge badge-in_progress',
    'complete': 'badge badge-done',
    'delivered': 'badge badge-done',
    'print-to_do': 'badge badge-todo',
    'print-awaiting_input': 'badge badge-awaiting_input',
    'print-queued': 'badge badge-queued',
    'print-printing': 'badge badge-in_progress',
    'print-complete': 'badge badge-printed',
  };
  return map[status] || 'badge badge-todo';
}

function statusLabel(s) {
  return {
    received: 'Received',
    awaiting_input: 'Awaiting Input',
    queued: 'Queued',
    in_process: 'Printing',
    complete: 'Complete',
    delivered: 'Delivered',
    todo: 'Queued',
    in_progress: 'Printing',
    done: 'Complete',
  }[s] || s;
}

function printStatusLabel(s) {
  return {
    to_do: 'To Do',
    awaiting_input: 'Awaiting Input',
    queued: 'Queued',
    printing: 'Printing',
    complete: 'Complete',
    todo: 'Queued',
    in_progress: 'Printing',
    printed: 'Complete',
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

  const progressDiv = document.getElementById('importProgress');
  const filesDiv = document.getElementById('importFiles');
  let currentTicketId = null;
  let summary = null;
  const importedByTicket = {};
  const ticketTitles = {};

  function renderImportedSummary() {
    const ticketIds = Object.keys(importedByTicket);
    if (!ticketIds.length) {
      filesDiv.innerHTML = '<div style="font-size: 12px; color: var(--text3);">No new prints were imported.</div>';
      return;
    }

    filesDiv.innerHTML = ticketIds.map(ticketId => {
      const ticket = importedByTicket[ticketId];
      const title = ticketTitles[ticketId] || `Ticket #${ticketId}`;
      return `
        <div style="margin-bottom: 12px;">
          <div style="font-size: 12px; font-weight: 600; margin-bottom: 4px;">${esc(title)} (${ticketId})</div>
          <div style="font-size: 11px; color: var(--text3);">Imported prints:</div>
          <ul style="margin: 6px 0 0 16px; padding: 0; list-style: disc; color: var(--text);">
            ${ticket.files.map(f => `<li>${esc(f)}</li>`).join('')}
          </ul>
        </div>
      `;
    }).join('');
  }

  try {
    const res = await fetch('/api/import-from-directory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    if (!res.body) throw new Error('No response body from import endpoint');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const msg = JSON.parse(line);
        if (msg.type === 'start') {
          progressDiv.innerHTML = esc(msg.message);
          continue;
        }
        if (msg.type === 'ticket') {
          currentTicketId = msg.ticket_id;
          progressDiv.innerHTML = `Importing ticket <strong>#${msg.ticket_id}</strong>...`;
          continue;
        }
        if (msg.type === 'file') {
          progressDiv.innerHTML = `Importing ticket <strong>#${msg.ticket_id}</strong>...<br>Processing <strong>${esc(msg.filename)}</strong>`;
          if (msg.status === 'completed') {
            importedByTicket[msg.ticket_id] = importedByTicket[msg.ticket_id] || { files: [] };
            importedByTicket[msg.ticket_id].files.push(msg.filename);
          }
          continue;
        }
        if (msg.type === 'summary') {
          summary = msg.summary;
          if (summary.tickets) {
            summary.tickets.forEach(t => { ticketTitles[t.id] = t.title; });
          }
        }
      }
    }

    if (buffer.trim()) {
      const msg = JSON.parse(buffer);
      if (msg.type === 'summary') {
        summary = msg.summary;
        if (summary.tickets) {
          summary.tickets.forEach(t => { ticketTitles[t.id] = t.title; });
        }
      }
    }

    progressDiv.innerHTML = 'Import complete!';
    renderImportedSummary();

    if (summary) {
      if (summary.created_tickets || summary.updated_tickets || summary.added_prints) {
        const msg = [
          summary.created_tickets > 0 && `${summary.created_tickets} new ticket(s)`,
          summary.updated_tickets > 0 && `${summary.updated_tickets} updated`,
          summary.added_prints > 0 && `${summary.added_prints} print(s) added`,
        ].filter(Boolean).join(', ');
        toast(`✓ Import complete: ${msg}`, 'success');
        await loadTickets();
      } else {
        toast('No new tickets or prints found', 'info');
      }
      if (summary.errors && summary.errors.length > 0) {
        toast(`⚠ ${summary.errors.length} error(s) during import`, 'warning');
        console.log('Import errors:', summary.errors);
      }
    }
  } catch (err) {
    progressDiv.innerHTML = 'Import failed!';
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
