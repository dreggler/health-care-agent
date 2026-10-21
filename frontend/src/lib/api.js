const API_BASE = '/api'


const TOKEN_STORAGE_KEY = 'hime.apiAuthToken'
const BUILD_TIME_TOKEN = import.meta.env.VITE_API_AUTH_TOKEN || ''

function _store(kind) {
  try {
    const s = kind === 'local' ? window.localStorage : window.sessionStorage
    return s && typeof s.getItem === 'function' ? s : null
  } catch (_) {
    return null
  }
}

function _read(kind) {
  try {
    return _store(kind)?.getItem(TOKEN_STORAGE_KEY) || ''
  } catch (_) {
    return ''
  }
}

export function getAuthToken() {
  return _read('session') || _read('local') || BUILD_TIME_TOKEN
}

export function setAuthToken(token, { remember = false } = {}) {
  const value = (token || '').trim()
  for (const kind of ['session', 'local']) {
    try {
      _store(kind)?.removeItem(TOKEN_STORAGE_KEY)
    } catch (_) { }
  }
  if (!value) return
  try {
    _store(remember ? 'local' : 'session')?.setItem(TOKEN_STORAGE_KEY, value)
  } catch (_) { }
}

export function clearAuthToken() {
  setAuthToken('')
}

export function reloadForAuth() {
  window.location.reload()
}


const _authListeners = new Set()

export function onAuthRequired(listener) {
  _authListeners.add(listener)
  return () => _authListeners.delete(listener)
}

function _noteAuthStatus(status) {
  if (status !== 401 && status !== 403) return
  for (const listener of Array.from(_authListeners)) {
    try {
      listener(status)
    } catch (e) {
      console.error('auth listener failed:', e)
    }
  }
}

function _authHeaders() {
  const h = {}
  const token = getAuthToken()
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}


async function _get(path, options = {}) {
  try {
    const { headers: extraHeaders, ...rest } = options
    const res = await fetch(`${API_BASE}${path}`, { ...rest, headers: { ...(extraHeaders || {}), ..._authHeaders() } })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      _noteAuthStatus(res.status)
      return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
    }
    return data
  } catch (e) {
    return { success: false, error: e?.message || 'Network error' }
  }
}

async function _put(path, body) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      _noteAuthStatus(res.status)
      return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
    }
    return data
  } catch (e) {
    return { success: false, error: e?.message || 'Network error' }
  }
}

async function _post(path, body) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      _noteAuthStatus(res.status)
      return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
    }
    return data
  } catch (e) {
    return { success: false, error: e?.message || 'Network error' }
  }
}

async function _patch(path, body) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      _noteAuthStatus(res.status)
      return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
    }
    return data
  } catch (e) {
    return { success: false, error: e?.message || 'Network error' }
  }
}

async function _delete(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`, { method: 'DELETE', headers: _authHeaders() })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      _noteAuthStatus(res.status)
      return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
    }
    return data
  } catch (e) {
    return { success: false, error: e?.message || 'Network error' }
  }
}


function _ws(path) {
  let url;
  if (API_BASE.startsWith('http')) {
    const wsBase = API_BASE.replace('http', 'ws');
    url = `${wsBase}${path}`;
  } else {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    url = `${proto}//${window.location.host}${API_BASE}${path}`;
  }
  const token = getAuthToken()
  if (token) {
    const sep = url.includes('?') ? '&' : '?'
    url += `${sep}token=${encodeURIComponent(token)}`
  }
  return new WebSocket(url);
}


export const api = {

  getDataSource: () => _get('/data/source'),

  reloadDataReader: () => _post('/data/reload', {}),

  getDataCount: (signal = null) => _get('/data/count', signal ? { signal } : {}),


  getParticipants: (datasets = null) => {
    const qs = datasets?.length ? '?' + datasets.map(d => `datasets=${d}`).join('&') : ''
    return _get(`/data/users${qs}`)
  },

  getFeatureTypes: () => _get('/data/feature_types'),

  getParticipantFeatures: (pid, featureType = 'steps') =>
    _get(`/data/features/${encodeURIComponent(pid)}?feature_type=${encodeURIComponent(featureType)}`),

  getFeatureMetadata: () => _get('/data/feature_metadata'),

  inspectParticipantData: async (pid, featureType = 'steps', limit = 100) => {
    try {
      const res = await fetch(
        `${API_BASE}/data/inspect/${encodeURIComponent(pid)}?feature_type=${encodeURIComponent(featureType)}&limit=${limit}`,
        { headers: _authHeaders() }
      )
      const text = (await res.text()).replace(/\bNaN\b|-?Infinity\b/g, 'null')
      let data = {}
      try { data = JSON.parse(text) } catch (_) { }
      if (!res.ok) {
        _noteAuthStatus(res.status)
        return { success: false, error: data.detail || data.error || `HTTP ${res.status}` }
      }
      return data
    } catch (e) {
      return { success: false, error: e?.message || 'Network error' }
    }
  },


  getStreamConfig: () => _get('/config/stream'),

  getDefaults: () => _get('/config/defaults'),

  setStreamConfig: (isStreaming = null, liveHistoryWindow = null) => {
    const body = { granularity: 'real-time' }
    if (isStreaming !== null) body.is_streaming = isStreaming
    if (liveHistoryWindow !== null) body.live_history_window = liveHistoryWindow
    return _post('/config/stream', body)
  },

  setParticipants: (userIds, datasets = null) =>
    _post('/config/users', { user_ids: userIds, datasets }),


  startAutonomousAgent: (llmProvider = 'gemini', options = {}) =>
    _post('/agent/start', {
      llm_provider: llmProvider,
      model: options.model || null,
    }),

  stopAutonomousAgent: () =>
    _post('/agent/stop', {}),

  stopChat: (threadId = null) =>
    _post('/agent/chat/stop', threadId ? { thread_id: threadId } : {}),

  getChatThreads: (includeArchived = false) =>
    _get(`/agent/chat/threads?include_archived=${includeArchived ? 'true' : 'false'}`),

  createChatThread: (title = '') =>
    _post('/agent/chat/threads', title ? { title } : {}),

  updateChatThread: (threadId, patch) =>
    _patch(`/agent/chat/threads/${encodeURIComponent(threadId)}`, patch),

  deleteChatThread: (threadId) =>
    _delete(`/agent/chat/threads/${encodeURIComponent(threadId)}`),

  getAgentLastConfig: () =>
    _get('/agent/last-config'),

  getAgentStatus: (userId = null) =>
    _get(userId
      ? `/agent/status?user_id=${encodeURIComponent(userId)}`
      : '/agent/status'),

  getAgentActivity: (limit = 500) =>
    _get(`/agent/activity/LiveUser?limit=${limit}`),

  queryAgentMemory: (queryType = 'stats') =>
    _get(`/agent/memory/LiveUser?query_type=${encodeURIComponent(queryType)}`),

  getTools: () => _get('/agent/tools'),

  inspectMemoryTable: (tableName, limit = 50) =>
    _get(`/agent/memory/LiveUser/inspect?table_name=${encodeURIComponent(tableName)}&limit=${limit}`),

  getScheduledTasks: () =>
    _get('/agent/scheduled-tasks/LiveUser'),

  createScheduledTask: (cronExpr, promptGoal) =>
    _post('/agent/scheduled-tasks/LiveUser', { cron_expr: cronExpr, prompt_goal: promptGoal }),

  updateScheduledTask: (taskId, updates) =>
    _put(`/agent/scheduled-tasks/LiveUser/${encodeURIComponent(taskId)}`, updates),

  triggerAnalysis: (goal = null) =>
    _post('/agent/trigger-analysis/LiveUser', { goal }),

  getTriggerRules: () =>
    _get('/agent/trigger-rules/LiveUser'),

  createTriggerRule: (rule) =>
    _post('/agent/trigger-rules/LiveUser', rule),

  updateTriggerRule: (ruleId, updates) =>
    _put(`/agent/trigger-rules/LiveUser/${encodeURIComponent(ruleId)}`, updates),


  connectDataStream: () => _ws('/stream/data'),

  connectAgentMonitor: () =>
    _ws('/stream/agent/LiveUser'),


  listPrompts: () => _get('/prompts'),
  fetchPrompt: (id) => _get(`/prompts/${encodeURIComponent(id)}`),
  savePrompt: (id, content) => _post(`/prompts/${encodeURIComponent(id)}`, { content }),


  listSkills: () => _get('/skills'),
  fetchSkill: (name) => _get(`/skills/${encodeURIComponent(name)}`),
  createSkill: (name, description, body) =>
    _post('/skills', { name, description, body }),
  updateSkill: (name, description, body) =>
    _put(`/skills/${encodeURIComponent(name)}`, { description, body }),
  deleteSkill: (name) => _delete(`/skills/${encodeURIComponent(name)}`),
  setSkillState: (disabled) => _put('/skills/state', { disabled }),


  listPersonalisedPages: () => _get('/personalised-pages/list'),

  getPersonalisedPageHtml: (pageId) =>
    fetch(`${API_BASE}/personalised-pages/${encodeURIComponent(pageId)}/`, {
      headers: _authHeaders(),
    })
      .then(async (res) => {
        if (!res.ok) {
          _noteAuthStatus(res.status)
          const data = await res.json().catch(() => ({}))
          return {
            success: false,
            status: res.status,
            error: data.detail || data.error || `HTTP ${res.status}`,
          }
        }
        return { success: true, html: await res.text() }
      })
      .catch((e) => ({ success: false, error: e?.message || 'Network error' })),

  fetchChatImage: async (urlOrId) => {
    try {
      const raw = String(urlOrId || '')
      const path = raw.startsWith('/api/')
        ? raw.slice(API_BASE.length)
        : `/agent/chat-image/${encodeURIComponent(raw)}`
      const res = await fetch(`${API_BASE}${path}`, { headers: _authHeaders() })
      if (!res.ok) {
        _noteAuthStatus(res.status)
        return { success: false, error: `HTTP ${res.status}` }
      }
      return { success: true, blob: await res.blob() }
    } catch (e) {
      return { success: false, error: e?.message || 'Network error' }
    }
  },

  deletePersonalisedPage: (pageId) => _delete(`/personalised-pages/${encodeURIComponent(pageId)}`),
}
