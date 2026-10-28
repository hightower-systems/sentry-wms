import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';
import { useWarehouse } from '../warehouse.jsx';

const EMPTY_BINDING = {
  ship_no: '',
  engineering_no: '',
  production_drawing_no: '',
  manufacturer_name: '',
  effective_from: '',
};

const EMPTY_MAPPING = {
  bin_id: '',
  ship_no: '',
  is_primary: true,
  effective_from: '',
};

async function requireJson(response, fallback) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.error || fallback);
  }
  return data;
}

function toIsoOrNull(value) {
  return value ? new Date(value).toISOString() : null;
}

function isoToLocalInput(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

async function loadSettingsData(warehouseId) {
  const [bindingResponse, mappingResponse, locationResponse] = await Promise.all([
    api.get('/shipyard/bindings?active_only=false'),
    api.get(`/shipyard/zone-mappings?warehouse_id=${warehouseId}&active_only=false`),
    api.get(`/shipyard/zone-mapping-options?warehouse_id=${warehouseId}`),
  ]);
  const [bindingData, mappingData, locationData] = await Promise.all([
    requireJson(bindingResponse, '无法读取船号绑定'),
    requireJson(mappingResponse, '无法读取区域定置'),
    requireJson(locationResponse, '无法读取库区选项'),
  ]);
  return {
    bindings: bindingData.bindings || [],
    mappings: mappingData.mappings || [],
    locations: locationData.locations || [],
  };
}

export default function ShipyardSettings() {
  const { warehouseId } = useWarehouse();
  const [bindings, setBindings] = useState([]);
  const [mappings, setMappings] = useState([]);
  const [locations, setLocations] = useState([]);
  const [bindingForm, setBindingForm] = useState(EMPTY_BINDING);
  const [mappingForm, setMappingForm] = useState(EMPTY_MAPPING);
  const [editingBinding, setEditingBinding] = useState(null);
  const [editingMapping, setEditingMapping] = useState(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);

  const locationsByZone = useMemo(() => {
    const grouped = new Map();
    for (const location of locations) {
      const key = `${location.zone_code} · ${location.zone_name}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(location);
    }
    return Array.from(grouped.entries());
  }, [locations]);

  const load = useCallback(async () => {
    if (!warehouseId) return;
    setBusy('load');
    try {
      const data = await loadSettingsData(warehouseId);
      setBindings(data.bindings);
      setMappings(data.mappings);
      setLocations(data.locations);
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }, [warehouseId]);

  useEffect(() => {
    if (!warehouseId) return undefined;
    let cancelled = false;
    loadSettingsData(warehouseId)
      .then((data) => {
        if (cancelled) return;
        setBindings(data.bindings);
        setMappings(data.mappings);
        setLocations(data.locations);
      })
      .catch((error) => {
        if (!cancelled) setNotice({ type: 'error', message: error.message });
      });
    return () => {
      cancelled = true;
    };
  }, [warehouseId]);

  function resetBinding() {
    setEditingBinding(null);
    setBindingForm(EMPTY_BINDING);
  }

  function editBinding(binding) {
    setEditingBinding(binding);
    setBindingForm({
      ship_no: binding.ship_no,
      engineering_no: binding.engineering_no,
      production_drawing_no: binding.production_drawing_no,
      manufacturer_name: binding.manufacturer_name,
      effective_from: isoToLocalInput(binding.effective_from),
    });
  }

  async function saveBinding(event) {
    event.preventDefault();
    setBusy('binding');
    setNotice(null);
    const body = {
      ship_no: bindingForm.ship_no,
      engineering_no: bindingForm.engineering_no,
      production_drawing_no: bindingForm.production_drawing_no,
      manufacturer_name: bindingForm.manufacturer_name,
    };
    if (bindingForm.effective_from) {
      body.effective_from = toIsoOrNull(bindingForm.effective_from);
    }
    try {
      const response = editingBinding
        ? await api.put(`/shipyard/bindings/${editingBinding.binding_id}`, body)
        : await api.post('/shipyard/bindings', body);
      await requireJson(response, '保存船号绑定失败');
      setNotice({
        type: 'success',
        message: editingBinding ? '船号绑定已更新' : '船号绑定已创建',
      });
      resetBinding();
      await load();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function setBindingStatus(binding, status) {
    setBusy(`binding-${binding.binding_id}`);
    try {
      const response = await api.put(`/shipyard/bindings/${binding.binding_id}`, {
        status,
        ...(status === 'ACTIVE' ? { effective_to: null } : {}),
      });
      await requireJson(response, '更新绑定状态失败');
      setNotice({
        type: 'success',
        message: status === 'ACTIVE' ? '绑定已启用' : '绑定已停用',
      });
      await load();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  function resetMapping() {
    setEditingMapping(null);
    setMappingForm(EMPTY_MAPPING);
  }

  function editMapping(mapping) {
    setEditingMapping(mapping);
    setMappingForm({
      bin_id: String(mapping.bin_id),
      ship_no: mapping.ship_no,
      is_primary: mapping.is_primary,
      effective_from: isoToLocalInput(mapping.effective_from),
    });
  }

  async function saveMapping(event) {
    event.preventDefault();
    if (!warehouseId) return;
    setBusy('mapping');
    setNotice(null);
    const body = {
      bin_id: Number(mappingForm.bin_id),
      ship_no: mappingForm.ship_no,
      is_primary: mappingForm.is_primary,
    };
    if (mappingForm.effective_from) {
      body.effective_from = toIsoOrNull(mappingForm.effective_from);
    }
    try {
      const response = editingMapping
        ? await api.put(`/shipyard/zone-mappings/${editingMapping.mapping_id}`, body)
        : await api.post('/shipyard/zone-mappings', {
            ...body,
            warehouse_id: warehouseId,
          });
      await requireJson(response, '保存区域定置失败');
      setNotice({
        type: 'success',
        message: editingMapping ? '区域定置已更新' : '区域定置已创建',
      });
      resetMapping();
      await load();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function setMappingStatus(mapping, status) {
    setBusy(`mapping-${mapping.mapping_id}`);
    try {
      const response = await api.put(`/shipyard/zone-mappings/${mapping.mapping_id}`, {
        status,
        ...(status === 'ACTIVE' ? { effective_to: null } : {}),
      });
      await requireJson(response, '更新区域状态失败');
      setNotice({
        type: 'success',
        message: status === 'ACTIVE' ? '区域定置已启用' : '区域定置已停用',
      });
      await load();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  if (!warehouseId) {
    return (
      <div>
        <PageHeader title="船厂后台设置" />
        <div className="settings-section">请先在顶部选择仓库。</div>
      </div>
    );
  }

  return (
    <div className="shipyard-page">
      <PageHeader title="船厂后台设置">
        <button className="btn" onClick={load} disabled={busy === 'load'}>
          {busy === 'load' ? '读取中…' : '刷新'}
        </button>
      </PageHeader>

      {notice && (
        <div className={`shipyard-notice shipyard-notice-${notice.type}`} role="status">
          <span>{notice.message}</span>
          <button className="btn btn-sm" onClick={() => setNotice(null)}>关闭</button>
        </div>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>全局船号 / 工程号 / 制作图绑定</h3>
            <p className="settings-note">
              所有仓库共享这组船号主数据。修改需要船厂主管角色和设置页权限，并会写入审计；
              仓库隔离适用于清单、票货、托盘、照片和区域定置。
            </p>
          </div>
        </div>
        <form className="shipyard-settings-form" onSubmit={saveBinding}>
          <label>
            <span>船号</span>
            <input
              className="input"
              required
              value={bindingForm.ship_no}
              onChange={(event) => setBindingForm({
                ...bindingForm,
                ship_no: event.target.value,
              })}
            />
          </label>
          <label>
            <span>工程号</span>
            <input
              className="input"
              value={bindingForm.engineering_no}
              onChange={(event) => setBindingForm({
                ...bindingForm,
                engineering_no: event.target.value,
              })}
            />
          </label>
          <label>
            <span>制作图编号</span>
            <input
              className="input"
              value={bindingForm.production_drawing_no}
              onChange={(event) => setBindingForm({
                ...bindingForm,
                production_drawing_no: event.target.value,
              })}
            />
          </label>
          <label>
            <span>厂家</span>
            <input
              className="input"
              value={bindingForm.manufacturer_name}
              onChange={(event) => setBindingForm({
                ...bindingForm,
                manufacturer_name: event.target.value,
              })}
            />
          </label>
          <label>
            <span>生效时间</span>
            <input
              className="input"
              type="datetime-local"
              value={bindingForm.effective_from}
              onChange={(event) => setBindingForm({
                ...bindingForm,
                effective_from: event.target.value,
              })}
            />
          </label>
          <div className="shipyard-actions">
            <button className="btn btn-primary" disabled={busy === 'binding'}>
              {editingBinding ? '保存修改' : '新增绑定'}
            </button>
            {editingBinding && (
              <button type="button" className="btn" onClick={resetBinding}>
                取消编辑
              </button>
            )}
          </div>
        </form>

        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>船号</th>
                <th>工程号</th>
                <th>制作图</th>
                <th>厂家</th>
                <th>生效时间</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {bindings.map((binding) => (
                <tr key={binding.binding_id}>
                  <td>{binding.ship_no}</td>
                  <td>{binding.engineering_no || '—'}</td>
                  <td>{binding.production_drawing_no || '—'}</td>
                  <td>{binding.manufacturer_name || '—'}</td>
                  <td>{binding.effective_from?.slice(0, 16).replace('T', ' ')}</td>
                  <td>
                    <span className={`shipyard-status shipyard-status-${binding.status.toLowerCase()}`}>
                      {binding.status === 'ACTIVE' ? '生效' : '停用'}
                    </span>
                  </td>
                  <td className="shipyard-actions">
                    <button className="btn btn-sm" onClick={() => editBinding(binding)}>
                      编辑
                    </button>
                    <button
                      className="btn btn-sm"
                      onClick={() => setBindingStatus(
                        binding,
                        binding.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE',
                      )}
                      disabled={busy === `binding-${binding.binding_id}`}
                    >
                      {binding.status === 'ACTIVE' ? '停用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
              {!bindings.length && (
                <tr><td colSpan="7" className="empty-state">还没有船号绑定</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>区域 / 船号定置</h3>
            <p className="settings-note">
              当前仓库：每条船号同一时间只能配置一个主入库区域。
            </p>
          </div>
        </div>
        <form className="shipyard-settings-form" onSubmit={saveMapping}>
          <label>
            <span>二级区域</span>
            <select
              className="input"
              required
              value={mappingForm.bin_id}
              onChange={(event) => setMappingForm({
                ...mappingForm,
                bin_id: event.target.value,
              })}
            >
              <option value="">选择区域</option>
              {locationsByZone.map(([zoneLabel, zoneLocations]) => (
                <optgroup key={zoneLabel} label={zoneLabel}>
                  {zoneLocations.map((location) => (
                    <option key={location.bin_id} value={location.bin_id}>
                      {location.location_path}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
          <label>
            <span>船号</span>
            <input
              className="input"
              required
              list="shipyard-setting-ships"
              value={mappingForm.ship_no}
              onChange={(event) => setMappingForm({
                ...mappingForm,
                ship_no: event.target.value,
              })}
            />
            <datalist id="shipyard-setting-ships">
              {[...new Set(bindings.map((binding) => binding.ship_no))].map((shipNo) => (
                <option key={shipNo} value={shipNo} />
              ))}
            </datalist>
          </label>
          <label className="shipyard-checkbox">
            <input
              type="checkbox"
              checked={mappingForm.is_primary}
              onChange={(event) => setMappingForm({
                ...mappingForm,
                is_primary: event.target.checked,
              })}
            />
            <span>作为主入库区域</span>
          </label>
          <label>
            <span>生效时间</span>
            <input
              className="input"
              type="datetime-local"
              value={mappingForm.effective_from}
              onChange={(event) => setMappingForm({
                ...mappingForm,
                effective_from: event.target.value,
              })}
            />
          </label>
          <div className="shipyard-actions">
            <button className="btn btn-primary" disabled={busy === 'mapping'}>
              {editingMapping ? '保存修改' : '新增定置'}
            </button>
            {editingMapping && (
              <button type="button" className="btn" onClick={resetMapping}>
                取消编辑
              </button>
            )}
          </div>
        </form>

        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>船号</th>
                <th>完整区域</th>
                <th>主区域</th>
                <th>生效时间</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {mappings.map((mapping) => (
                <tr key={mapping.mapping_id}>
                  <td>{mapping.ship_no}</td>
                  <td>{mapping.location_path}</td>
                  <td>{mapping.is_primary ? '是' : '否'}</td>
                  <td>{mapping.effective_from?.slice(0, 16).replace('T', ' ')}</td>
                  <td>
                    <span className={`shipyard-status shipyard-status-${mapping.status.toLowerCase()}`}>
                      {mapping.status === 'ACTIVE' ? '生效' : '停用'}
                    </span>
                  </td>
                  <td className="shipyard-actions">
                    <button className="btn btn-sm" onClick={() => editMapping(mapping)}>
                      编辑
                    </button>
                    <button
                      className="btn btn-sm"
                      onClick={() => setMappingStatus(
                        mapping,
                        mapping.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE',
                      )}
                      disabled={busy === `mapping-${mapping.mapping_id}`}
                    >
                      {mapping.status === 'ACTIVE' ? '停用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
              {!mappings.length && (
                <tr><td colSpan="6" className="empty-state">还没有区域定置</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
