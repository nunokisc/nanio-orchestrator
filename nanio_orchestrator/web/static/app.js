/* nanio-orchestrator — Web UI JavaScript */

// Auth is handled via session cookie — no API key needed in JS.
// All fetch calls use credentials:'same-origin' so the browser
// sends the nanio_session cookie automatically.
function getHeaders() {
    return { 'Content-Type': 'application/json' };
}

function fetchOpts(method, body) {
    const opts = { method, headers: getHeaders(), credentials: 'same-origin' };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return opts;
}

// ── Modal helpers ────────────────────────────────────────────────────────────

function showModal(id) {
    document.getElementById(id).classList.remove('hidden');
}

function hideModal(id) {
    document.getElementById(id).classList.add('hidden');
}

// Close modal on backdrop click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.add('hidden');
    }
});

// ── Pool operations ──────────────────────────────────────────────────────────

async function createPool(e) {
    e.preventDefault();
    const form = e.target;
    const data = {
        name: form.name.value,
        description: form.description.value || null,
        type: form.type.value,
        lb_method: form.lb_method.value,
        keepalive: parseInt(form.keepalive.value),
    };

    try {
        const res = await fetch('/api/pools', {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function deletePool(id, name) {
    if (!confirm(`Delete pool "${name}"? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/pools/${id}`, {
            method: 'DELETE',
            headers: getHeaders(),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function previewPool(id) {
    try {
        const res = await fetch(`/api/config/preview/pool/${id}`, { headers: getHeaders() });
        const data = await res.json();
        document.getElementById('preview-content').textContent = data.content;
        showModal('preview-modal');
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

// ── Member operations ────────────────────────────────────────────────────────

function showAddMember(poolId, poolType) {
    document.getElementById('member-pool-id').value = poolId;
    const roleSelect = document.getElementById('member-role-select');
    // Reset options based on pool type
    roleSelect.innerHTML = '';
    if (poolType === 'nanio') {
        roleSelect.innerHTML = '<option value="active">active</option>';
    } else {
        roleSelect.innerHTML = '<option value="primary">primary</option><option value="replica">replica</option>';
    }
    showModal('add-member-modal');
}

async function addMember(e) {
    e.preventDefault();
    const form = e.target;
    const poolId = form.pool_id.value;
    const data = {
        address: form.address.value,
        role: form.role.value,
        weight: parseInt(form.weight.value),
        max_fails: parseInt(form.max_fails.value),
        fail_timeout_s: parseInt(form.fail_timeout_s.value),
    };

    try {
        const res = await fetch(`/api/pools/${poolId}/members`, {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function deleteMember(poolId, memberId) {
    if (!confirm('Delete this member?')) return;
    try {
        const res = await fetch(`/api/pools/${poolId}/members/${memberId}`, {
            method: 'DELETE',
            headers: getHeaders(),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

// ── Node setup ───────────────────────────────────────────────────────────────

function toggleNanioFields() {
    const type = document.getElementById('node-type-select').value;
    const nanioFields = document.getElementById('nanio-fields');
    if (type === 'nginx-only') {
        nanioFields.style.display = 'none';
    } else {
        nanioFields.style.display = '';
    }
}

function showNodeSetup(poolId, memberId, address, poolType) {
    document.getElementById('node-pool-id').value = poolId;
    document.getElementById('node-member-id').value = memberId;
    document.getElementById('node-setup-address').textContent = address;
    document.getElementById('node-config-output').classList.add('hidden');

    // Set appropriate defaults based on pool type
    const typeSelect = document.getElementById('node-type-select');
    typeSelect.innerHTML = '';
    if (poolType === 'nanio') {
        typeSelect.innerHTML = '<option value="nanio-only">nanio-only</option><option value="nginx-nanio">nginx + nanio</option>';
    } else {
        typeSelect.innerHTML = '<option value="nginx-only">nginx-only</option>';
    }
    toggleNanioFields();

    showModal('node-setup-modal');
}

async function generateNodeConfig(e) {
    e.preventDefault();
    const form = e.target;
    const poolId = form.pool_id.value;
    const memberId = form.member_id.value;
    const nodeType = form.node_type.value;
    const data = {
        node_type: nodeType,
        data_dir: form.data_dir.value,
    };
    if (nodeType !== 'nginx-only') {
        data.nanio_port = parseInt(form.nanio_port.value);
        data.nanio_host = form.nanio_host.value;
        data.nanio_region = form.nanio_region.value;
        data.access_key = form.access_key.value || null;
        data.secret_key = form.secret_key.value || null;
    }

    try {
        const res = await fetch(`/api/pools/${poolId}/members/${memberId}/node-config`, {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        const result = await res.json();

        // Render files
        const filesDiv = document.getElementById('node-config-files');
        filesDiv.innerHTML = '';
        for (const file of result.files) {
            const block = document.createElement('div');
            block.innerHTML = `<h5><code>${file.path}</code></h5><pre class="output">${escapeHtml(file.content)}</pre>`;
            filesDiv.appendChild(block);
        }
        document.getElementById('node-config-instructions').textContent = result.instructions;
        document.getElementById('node-config-output').classList.remove('hidden');
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

// ── Vhost operations ─────────────────────────────────────────────────────────

async function createVhost(e) {
    e.preventDefault();
    const form = e.target;
    const rawPoolId = form.default_pool_id ? form.default_pool_id.value : '';
    const data = {
        server_name: form.server_name.value,
        listen_port: parseInt(form.listen_port.value),
        ssl: form.ssl.checked,
        ssl_cert_path: form.ssl_cert_path.value || null,
        ssl_key_path: form.ssl_key_path.value || null,
        extra_directives: form.extra_directives.value || null,
        default_pool_id: rawPoolId ? parseInt(rawPoolId) : null,
    };

    try {
        const res = await fetch('/api/vhosts', {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function deleteVhost(id, name) {
    if (!confirm(`Delete vhost "${name}"? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/vhosts/${id}`, {
            method: 'DELETE',
            headers: getHeaders(),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function previewVhost(id) {
    try {
        const res = await fetch(`/api/config/preview/vhost/${id}`, { headers: getHeaders() });
        const data = await res.json();
        document.getElementById('preview-content').textContent = data.content;
        showModal('preview-modal');
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

// ── Route operations ─────────────────────────────────────────────────────────

function showAddRoute(vhostId) {
    document.getElementById('route-vhost-id').value = vhostId;
    showModal('add-route-modal');
}

async function addRoute(e) {
    e.preventDefault();
    const form = e.target;
    const vhostId = form.vhost_id.value;
    const data = {
        path_prefix: form.path_prefix.value,
        pool_id: parseInt(form.pool_id.value),
        extra_directives: form.extra_directives.value || null,
    };

    try {
        const res = await fetch(`/api/vhosts/${vhostId}/routes`, {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        const result = await res.json();
        hideModal('add-route-modal');

        // Offer migration if the bucket has objects on the source (default) pool
        if (result.objects_on_source > 0 && result.bucket && result.default_pool_id) {
            const msg = result.bucket_provisioned
                ? `Route created. Bucket "${result.bucket}" was provisioned on the target pool.\n\n`
                : `Route created.\n\n`;
            const doMigrate = confirm(
                msg +
                `The bucket has ${result.objects_on_source} object(s) on the default pool.\n` +
                `Start rclone migration to the new pool now?`
            );
            if (doMigrate) {
                const migRes = await fetch('/api/migrations', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({
                        bucket: result.bucket,
                        src_pool_id: result.default_pool_id,
                        dst_pool_id: data.pool_id,
                    }),
                });
                if (migRes.ok) {
                    alert('Migration started. Track progress on the Migrations page.');
                } else {
                    const err = await migRes.json();
                    alert('Migration failed to start: ' + (err.detail || JSON.stringify(err)));
                }
            }
        }

        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

async function deleteRoute(vhostId, routeId) {
    if (!confirm('Delete this route?')) return;
    try {
        const res = await fetch(`/api/vhosts/${vhostId}/routes/${routeId}`, {
            method: 'DELETE',
            headers: getHeaders(),
        });
        if (!res.ok) {
            const err = await res.json();
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        location.reload();
    } catch (err) {
        alert('Network error: ' + err.message);
    }
}

// ── Config operations ────────────────────────────────────────────────────────

function showConfigResult(ok, output) {
    const container = document.getElementById('config-result');
    const card = document.getElementById('config-result-card');
    const pre = document.getElementById('config-result-output');
    container.classList.remove('hidden');
    card.className = ok ? 'card card-ok' : 'card card-error';
    pre.textContent = output;
}

async function validateConfig() {
    try {
        const res = await fetch('/api/config/validate', {
            method: 'POST',
            headers: getHeaders(),
        });
        const data = await res.json();
        showConfigResult(data.ok, data.output);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function rebuildConfig() {
    if (!confirm('Rebuild all config files from DB? This will overwrite existing files.')) return;
    try {
        const res = await fetch('/api/config/rebuild', {
            method: 'POST',
            headers: getHeaders(),
        });
        const data = await res.json();
        let msg = `OK: ${data.ok}\nWritten: ${(data.written || []).join(', ')}`;
        if (data.removed && data.removed.length) msg += `\nRemoved (empty pools): ${data.removed.join(', ')}`;
        msg += `\n${data.output || ''}`;
        showConfigResult(data.ok, msg);
        if (data.ok) setTimeout(() => location.reload(), 1200);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function absorbFile(path) {
    if (!confirm(`Absorb drift for:\n${path}\n\nThis will update the DB to match the current file on disk.`)) return;
    try {
        const res = await fetch('/api/config/absorb-file', {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify({ path }),
        });
        const data = await res.json();
        showConfigResult(data.ok, data.ok ? `Absorbed ${path}` : (data.detail || 'Error'));
        if (data.ok) setTimeout(() => location.reload(), 800);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function rewriteFile(path) {
    if (!confirm(`Rewrite from DB:\n${path}\n\nThis will overwrite the file on disk with the DB state.`)) return;
    try {
        const res = await fetch('/api/config/rewrite-file', {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify({ path }),
        });
        const data = await res.json();
        const msg = data.ok
            ? `Rewritten: ${path}\n${data.output || ''}`
            : (data.output || data.detail || 'Error');
        showConfigResult(data.ok, msg);
        if (data.ok) setTimeout(() => location.reload(), 1000);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function syncConfig() {
    try {
        const res = await fetch('/api/config/sync', {
            method: 'POST',
            headers: getHeaders(),
        });
        const data = await res.json();
        showConfigResult(true, `Imported ${data.count} files:\n${(data.imported || []).join('\n')}`);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function reloadNginx() {
    try {
        const res = await fetch('/api/config/reload', {
            method: 'POST',
            headers: getHeaders(),
        });
        const data = await res.json();
        showConfigResult(data.ok, data.output);
    } catch (err) {
        showConfigResult(false, 'Error: ' + err.message);
    }
}

async function viewFileContent(path) {
    // Use the config status to get content from DB
    try {
        const res = await fetch('/api/config/status', { headers: getHeaders() });
        const data = await res.json();
        const file = data.files.find(f => f.path === path);
        if (file) {
            document.getElementById('file-modal-title').textContent = path;
            document.getElementById('file-modal-content').textContent =
                `SHA256 (disk): ${file.sha256_disk || 'N/A'}\nSHA256 (DB):   ${file.sha256_db || 'N/A'}\nDrifted: ${file.drifted}\nLast synced: ${file.last_synced_at || 'N/A'}`;
            showModal('file-modal');
        }
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

// ── Audit detail ─────────────────────────────────────────────────────────────

function showAuditDetail(id) {
    const el = document.getElementById('audit-data-' + id);
    if (!el) return;
    const before = el.dataset.before;
    const after = el.dataset.after;
    const output = el.dataset.output;

    document.getElementById('audit-before').textContent = before ? JSON.stringify(JSON.parse(before), null, 2) : '—';
    document.getElementById('audit-after').textContent = after ? JSON.stringify(JSON.parse(after), null, 2) : '—';
    document.getElementById('audit-nginx-output').textContent = output || '—';
    showModal('audit-detail-modal');
}

// ── Bucket sync ──────────────────────────────────────────────────────────────

async function syncBuckets(vhostId) {
    try {
        const res = await fetch(`/api/vhosts/${vhostId}/buckets/sync`, {
            method: 'POST',
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        if (data.error) {
            alert('Sync error: ' + data.error);
        } else if (data.skipped) {
            alert('Skipped: ' + data.reason);
        } else {
            alert(`Synced ${data.buckets_found} buckets`);
            location.reload();
        }
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

function showRouteModal(vhostId, bucket) {
    document.getElementById('route-bucket-vhost-id').value = vhostId;
    document.getElementById('route-bucket-bucket').value = bucket;
    document.getElementById('route-bucket-name').textContent = bucket;
    showModal('route-bucket-modal');
}

async function promoteBucket(e) {
    e.preventDefault();
    const form = e.target;
    const vhostId = form.vhost_id.value;
    const bucket = form.bucket.value;
    const poolId = parseInt(form.pool_id.value);
    const migrate = form.migrate.checked;

    try {
        const res = await fetch(`/api/vhosts/${vhostId}/buckets/${encodeURIComponent(bucket)}/promote`, {
            method: 'POST',
            headers: getHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ pool_id: poolId, migrate }),
        });
        const data = await res.json();
        hideModal('route-bucket-modal');
        if (data.ok) {
            alert(`Bucket "${bucket}" routed to pool "${data.pool}".${migrate ? '\nMigration started.' : ''}`);
            location.reload();
        } else {
            alert('Error: ' + (data.error || data.detail || JSON.stringify(data)));
        }
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

async function ignoreBucket(vhostId, bucket) {
    if (!confirm(`Ignore bucket "${bucket}"? It won't appear as unrouted.`)) return;
    try {
        const res = await fetch(`/api/vhosts/${vhostId}/buckets/${encodeURIComponent(bucket)}/ignore`, {
            method: 'POST',
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        if (data.ok) location.reload();
        else alert('Error: ' + (data.detail || JSON.stringify(data)));
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Migrations ───────────────────────────────────────────────────────────────

async function cancelMigration(id) {
    if (!confirm(`Cancel migration #${id}?`)) return;
    try {
        const res = await fetch(`/api/migrations/${id}/cancel`, {
            method: 'POST',
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        if (data.ok) location.reload();
        else alert('Error: ' + (data.detail || JSON.stringify(data)));
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

async function createMigration(event) {
    event.preventDefault();
    const form = event.target;
    const body = {
        bucket: form.bucket.value,
        src_pool_id: parseInt(form.src_pool_id.value),
        dst_pool_id: parseInt(form.dst_pool_id.value),
    };
    try {
        const res = await fetch('/api/migrations', {
            method: 'POST',
            headers: { ...getHeaders(), 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) location.reload();
        else alert('Error: ' + (data.detail || JSON.stringify(data)));
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

async function showMigrationLog(id) {
    document.getElementById('migration-log-id').textContent = `#${id}`;
    const content = document.getElementById('migration-log-content');
    content.innerHTML = '<p>Loading...</p>';
    showModal('migration-log-modal');
    try {
        const res = await fetch(`/api/migrations/${id}/log`, {
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        const entries = await res.json();
        if (entries.length === 0) {
            content.innerHTML = '<p>No log entries</p>';
        } else {
            content.innerHTML = '<table><thead><tr><th>Time</th><th>Phase</th><th>Message</th></tr></thead><tbody>' +
                entries.map(e =>
                    `<tr><td>${escapeHtml(e.created_at)}</td><td><code>${escapeHtml(e.phase)}</code></td><td>${escapeHtml(e.message)}</td></tr>`
                ).join('') + '</tbody></table>';
        }
    } catch (err) {
        content.innerHTML = `<p class="error">Error: ${escapeHtml(err.message)}</p>`;
    }
}

// ── Pool Credentials ─────────────────────────────────────────────────────────

async function showCredentials(poolId) {
    try {
        const res = await fetch(`/api/pools/${poolId}/credentials`, {
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert('Error: ' + (err.detail || res.statusText));
            return;
        }
        const data = await res.json();
        const sourceLabel = data.source === 'global'
            ? '⚠ global fallback (no pool-specific credentials set)'
            : '✓ pool-specific';
        alert(
            `Pool #${poolId} — effective S3 credentials\n\n` +
            `Source:     ${sourceLabel}\n` +
            `Access Key: ${data.access_key_masked}\n` +
            `Region:     ${data.region}\n` +
            `Endpoint:   ${data.endpoint_url || '(default)'}`
        );
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

async function setCredentials(poolId) {
    const access = prompt('S3 Access Key:');
    if (!access) return;
    const secret = prompt('S3 Secret Key:');
    if (!secret) return;
    const endpoint = prompt('Endpoint URL (leave empty for default):') || null;
    const region = prompt('Region:', 'us-east-1') || 'us-east-1';

    try {
        const res = await fetch(`/api/pools/${poolId}/credentials`, {
            method: 'PUT',
            headers: { ...getHeaders(), 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ access_key: access, secret_key: secret, endpoint_url: endpoint, region }),
        });
        const data = await res.json();
        if (res.ok) {
            alert('Credentials saved (encrypted).');
            location.reload();
        } else {
            alert('Error: ' + (data.detail || JSON.stringify(data)));
        }
    } catch (err) {
        alert('Error: ' + err.message);
    }
}

async function deleteCredentials(poolId) {
    if (!confirm(`Delete credentials for pool #${poolId}?`)) return;
    try {
        const res = await fetch(`/api/pools/${poolId}/credentials`, {
            method: 'DELETE',
            headers: getHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        if (data.ok) {
            alert('Credentials deleted.');
            location.reload();
        } else {
            alert('Error: ' + (data.detail || JSON.stringify(data)));
        }
    } catch (err) {
        alert('Error: ' + err.message);
    }
}
