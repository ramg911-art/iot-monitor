/** Dahua NVR UI module - admin form, NVR list, camera grid integration. */

function getChannelFromExtra(extra) {
  if (!extra) return null;
  try {
    const o = typeof extra === 'string' ? JSON.parse(extra) : extra;
    return o.channel != null ? o.channel : null;
  } catch (_) { return null; }
}

function renderNvrList(devices, api, loadAll) {
  const nvrs = devices.filter(d => d.device_type === 'nvr');
  const cameras = devices.filter(d => d.device_type === 'nvr_camera');
  const listEl = document.getElementById('nvr-list');
  if (!listEl) return;
  if (nvrs.length === 0) {
    listEl.innerHTML = '<p style="color:var(--muted);font-size:0.85rem">No NVRs added yet.</p>';
    return;
  }
  listEl.innerHTML = nvrs.map(nvr => {
    const children = cameras.filter(c => c.parent_device_id === nvr.id);
    const nvrNameEsc = (nvr.name || '').replace(/"/g, '&quot;');
    return `
      <div class="nvr-item" data-nvr-id="${nvr.id}">
        <h4>
          <span class="status ${nvr.online ? 'online' : 'offline'}"></span>${nvr.name} ${nvr.ip_address ? '(' + nvr.ip_address + ')' : ''}
          <button class="delete-btn" data-id="${nvr.id}" data-name="${nvrNameEsc}" style="margin-left:8px;padding:2px 8px;font-size:0.7rem">Delete</button>
        </h4>
        ${children.length ? children.map(cam => {
          const camNameEsc = (cam.name || '').replace(/"/g, '&quot;');
          return `
          <div class="nvr-camera-row" data-cam-id="${cam.id}">
            <span class="channel-info">
              <span class="status ${cam.online ? 'online' : 'offline'}"></span>
              Ch${getChannelFromExtra(cam.extra_data) || '-'}: ${cam.name}
            </span>
            <span>
              <button class="toggle-btn ${cam.state === 'on' ? '' : 'off'}" data-cam-id="${cam.id}" style="padding:4px 10px;font-size:0.75rem">${cam.state === 'on' ? 'ON' : 'OFF'}</button>
              <button class="delete-btn" data-id="${cam.id}" data-name="${camNameEsc}" style="margin-left:4px;padding:2px 8px;font-size:0.7rem">Delete</button>
            </span>
          </div>
        `}).join('') : '<p style="color:var(--muted);font-size:0.8rem;margin:0">No channels discovered</p>'}
      </div>
    `;
  }).join('');
  listEl.querySelectorAll('.nvr-item .delete-btn').forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      const name = btn.dataset.name || 'device';
      if (!confirm('Delete "' + name + '"? This cannot be undone.')) return;
      const r = await api('DELETE', '/devices/' + id);
      if (r && r.deleted) loadAll();
      else alert(r?.detail || 'Delete failed');
    };
  });
  listEl.querySelectorAll('.nvr-camera-row .toggle-btn').forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.camId;
      const isOn = btn.classList.contains('off');
      await api('POST', isOn ? `/nvr/camera/${id}/enable` : `/nvr/camera/${id}/disable`);
      loadAll();
    };
  });
}

function renderNvrCameraTiles(streams) {
  return (streams || []).map(s => {
    const isNvr = s.type === 'nvr_camera';
    const overlayCls = isNvr && !s.online ? 'stream-overlay offline' : 'stream-overlay';
    const overlayHtml = isNvr
      ? `<div class="${overlayCls}"><span class="status ${s.online ? 'online' : 'offline'}"></span>${s.name}${s.parent_name ? ' · ' + s.parent_name : ''}${!s.online ? ' (offline)' : ''}</div>`
      : '';
    return `
      <div class="camera-frame" data-stream-id="${s.id}" data-device-id="${s.device_id || ''}" data-online="${s.online !== false}">
        ${overlayHtml}
        <iframe src="${s.url}" title="${s.name}" ${!s.online && isNvr ? 'style="opacity:0.3"' : ''}></iframe>
      </div>
    `;
  }).join('');
}
