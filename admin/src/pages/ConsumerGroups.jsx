import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

export default function ConsumerGroups() {
  const [groups, setGroups] = useState([]);
  const [connectors, setConnectors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState('');

  const [showCreateGroup, setShowCreateGroup] = useState(false);
  const [groupForm, setGroupForm] = useState({
    consumer_group_id: '',
    connector_id: '',
    subscription: '{}',
  });
  const [groupError, setGroupError] = useState('');

  const [showCreateConnector, setShowCreateConnector] = useState(false);
  const [connectorForm, setConnectorForm] = useState({
    connector_id: '',
    display_name: '',
  });
  const [connectorError, setConnectorError] = useState('');

  const [editGroup, setEditGroup] = useState(null);
  const [editSubscription, setEditSubscription] = useState('{}');
  const [editError, setEditError] = useState('');

  const [confirmDelete, setConfirmDelete] = useState(null);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    setLoading(true);
    const [cgRes, coRes] = await Promise.all([
      api.get('/admin/consumer-groups'),
      api.get('/admin/connector-registry'),
    ]);
    if (cgRes?.ok) {
      setGroups((await cgRes.json()).consumer_groups || []);
    } else {
      setPageError('Failed to load consumer groups');
    }
    if (coRes?.ok) {
      setConnectors((await coRes.json()).connectors || []);
    } else {
      setPageError('Failed to load connector registry');
    }
    setLoading(false);
  }

  function openCreateGroup() {
    setGroupForm({
      consumer_group_id: '',
      connector_id: connectors[0]?.connector_id || '',
      subscription: '{}',
    });
    setGroupError('');
    setShowCreateGroup(true);
  }

  function openCreateConnector() {
    setConnectorForm({ connector_id: '', display_name: '' });
    setConnectorError('');
    setShowCreateConnector(true);
  }

  function openEditGroup(g) {
    setEditGroup(g);
    setEditSubscription(JSON.stringify(g.subscription || {}, null, 2));
    setEditError('');
  }

  async function submitCreateConnector() {
    setConnectorError('');
    if (!connectorForm.connector_id.trim() || !connectorForm.display_name.trim()) {
      setConnectorError('Both fields are required'); return;
    }
    const res = await api.post('/admin/connector-registry', {
      connector_id: connectorForm.connector_id.trim(),
      display_name: connectorForm.display_name.trim(),
    });
    const body = await res?.json();
    if (res?.ok) {
      setShowCreateConnector(false);
      loadAll();
    } else {
      setConnectorError(body?.error || 'Failed to create connector');
    }
  }

  async function submitCreateGroup() {
    setGroupError('');
    if (!groupForm.consumer_group_id.trim()) { setGroupError('Group ID is required'); return; }
    if (!groupForm.connector_id) { setGroupError('Connector is required'); return; }
    let parsed;
    try {
      parsed = JSON.parse(groupForm.subscription || '{}');
    } catch {
      setGroupError('Subscription must be valid JSON'); return;
    }
    if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
      setGroupError('Subscription must be a JSON object'); return;
    }
    const res = await api.post('/admin/consumer-groups', {
      consumer_group_id: groupForm.consumer_group_id.trim(),
      connector_id: groupForm.connector_id,
      subscription: parsed,
    });
    const body = await res?.json();
    if (res?.ok) {
      setShowCreateGroup(false);
      loadAll();
    } else {
      setGroupError(body?.error || 'Failed to create group');
    }
  }

  async function submitEdit() {
    setEditError('');
    let parsed;
    try {
      parsed = JSON.parse(editSubscription || '{}');
    } catch {
      setEditError('Subscription must be valid JSON'); return;
    }
    if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
      setEditError('Subscription must be a JSON object'); return;
    }
    const res = await api.patch(`/admin/consumer-groups/${editGroup.consumer_group_id}`, {
      subscription: parsed,
    });
    const body = await res?.json();
    if (res?.ok) {
      setEditGroup(null);
      loadAll();
    } else {
      setEditError(body?.error || 'Failed to update');
    }
  }

  async function deleteGroup(g) {
    const res = await api.delete(`/admin/consumer-groups/${g.consumer_group_id}`);
    if (res?.ok) {
      setConfirmDelete(null);
      loadAll();
    } else {
      const body = await res?.json();
      setConfirmDelete(null);
      setPageError(body?.error || 'Delete failed');
    }
  }

  const columns = [
    { key: 'consumer_group_id', label: 'Group ID', mono: true },
    { key: 'connector_id', label: 'Connector', mono: true },
    { key: 'last_cursor', label: 'Cursor' },
    {
      key: 'subscription',
      label: 'Subscription',
      render: (r) => {
        const s = r.subscription || {};
        const keys = Object.keys(s);
        if (keys.length === 0) return <span style={{ color: 'var(--text-secondary)' }}>—</span>;
        return (
          <span className="mono" style={{ fontSize: 12 }}>
            {keys.map((k) => `${k}: ${Array.isArray(s[k]) ? `[${s[k].join(',')}]` : JSON.stringify(s[k])}`).join('; ')}
          </span>
        );
      },
    },
    { key: 'last_heartbeat', label: 'Heartbeat', render: (r) => formatDate(r.last_heartbeat) },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <div style={{ display: 'flex', gap: 4 }}>
          <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEditGroup(r); }}
                  aria-label="Edit" title="Edit subscription">&#9998;</button>
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); setConfirmDelete(r); }}
                  aria-label="Delete" title="Delete">&#128465;</button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Consumer groups">
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={openCreateConnector}>New connector</button>
          <button className="btn btn-primary" onClick={openCreateGroup} disabled={connectors.length === 0}>
            New group
          </button>
        </div>
      </PageHeader>

      {pageError && <div className="form-error" style={{ marginBottom: 12 }}>{pageError}</div>}
      {connectors.length === 0 && !loading && (
        <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          Create a connector first to provision consumer groups.
        </div>
      )}

      <DataTable
        columns={columns}
        data={groups}
        emptyMessage={loading ? 'Loading…' : 'No consumer groups yet'}
      />

      {showCreateGroup && (
        <Modal
          title="New consumer group"
          onClose={() => setShowCreateGroup(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreateGroup(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={submitCreateGroup}>Create</button>
            </>
          }
        >
          {groupError && <div className="form-error" style={{ marginBottom: 12 }}>{groupError}</div>}
          <div className="form-group">
            <label>Group ID</label>
            <input
              className="form-input"
              value={groupForm.consumer_group_id}
              onChange={(e) => setGroupForm({ ...groupForm, consumer_group_id: e.target.value })}
              placeholder="fabric-prod-main"
            />
          </div>
          <div className="form-group">
            <label>Connector</label>
            <select
              className="form-input"
              value={groupForm.connector_id}
              onChange={(e) => setGroupForm({ ...groupForm, connector_id: e.target.value })}
            >
              {connectors.map((c) => (
                <option key={c.connector_id} value={c.connector_id}>
                  {c.display_name} ({c.connector_id})
                </option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Subscription (JSON object)</label>
            <textarea
              className="form-input"
              rows={6}
              value={groupForm.subscription}
              onChange={(e) => setGroupForm({ ...groupForm, subscription: e.target.value })}
              style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}
            />
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
              Optional. Supported keys: <code>event_types</code>, <code>warehouse_ids</code>.
              Unknown keys are stored but ignored on the hot path.
            </div>
          </div>
        </Modal>
      )}

      {showCreateConnector && (
        <Modal
          title="New connector"
          onClose={() => setShowCreateConnector(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreateConnector(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={submitCreateConnector}>Create</button>
            </>
          }
        >
          {connectorError && <div className="form-error" style={{ marginBottom: 12 }}>{connectorError}</div>}
          <div className="form-group">
            <label>Connector ID</label>
            <input
              className="form-input"
              value={connectorForm.connector_id}
              onChange={(e) => setConnectorForm({ ...connectorForm, connector_id: e.target.value })}
              placeholder="fabric"
            />
          </div>
          <div className="form-group">
            <label>Display name</label>
            <input
              className="form-input"
              value={connectorForm.display_name}
              onChange={(e) => setConnectorForm({ ...connectorForm, display_name: e.target.value })}
              placeholder="Fabric Production"
            />
          </div>
        </Modal>
      )}

      {editGroup && (
        <Modal
          title={`Edit subscription: ${editGroup.consumer_group_id}`}
          onClose={() => setEditGroup(null)}
          footer={
            <>
              <button className="btn" onClick={() => setEditGroup(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={submitEdit}>Save</button>
            </>
          }
        >
          {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}
          <div className="form-group">
            <label>Subscription (JSON object)</label>
            <textarea
              className="form-input"
              rows={8}
              value={editSubscription}
              onChange={(e) => setEditSubscription(e.target.value)}
              style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}
            />
          </div>
        </Modal>
      )}

      {confirmDelete && (
        <Modal
          title="Delete consumer group"
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
              <button className="btn btn-primary" style={{ background: 'var(--copper)' }}
                      onClick={() => deleteGroup(confirmDelete)}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--danger)' }}>
            Delete {confirmDelete.consumer_group_id}? Connectors that rely on
            this group for cursor state will start a fresh scan from event_id=0
            on their next poll.
          </p>
        </Modal>
      )}
    </div>
  );
}
