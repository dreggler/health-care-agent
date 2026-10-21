const TOOL_EVENT_RE = /^(chat|analysis|quick|plan)_tool_(call|result)$/

const TIMELINE_TYPES = new Set([
  'user_message', 'chat_reply', 'chat_image', 'chat_stopped', 'chat_cleared',
  'thought', 'draft_held', 'chat_verification', 'token_usage',
  'cycle_start', 'cycle_end', 'report_pushed',
  'quick_analysis_start', 'quick_analysis_complete',
  'agent_started', 'agent_stopped', 'agent_error', 'startup_error', 'agent_waiting',
  'error', 'forced_sleep', 'token_truncated',
  'analysis_deferred', 'analysis_released',
])

export const HIDDEN_TOOLS = new Set(['reply_user', 'finish_chat'])

const GAP_CHAT_MS = 10 * 60 * 1000
const GAP_BG_MS = 5 * 60 * 1000
const STALE_MS = 15 * 60 * 1000
const BG_TAIL_MS = 20 * 1000
const DRAFT_MERGE_MS = 20 * 1000
const HELD_VERDICTS = new Set(['unverified', 'fabricated', 'rejected', 'blocked'])
const IDLE_GRACE_MS = 8000

export function isTimelineType(type) {
  return TIMELINE_TYPES.has(type) || TOOL_EVENT_RE.test(type)
}

export function toTimelineRecord(type, d, { id, key, ts, note } = {}) {
  if (!type || !isTimelineType(type)) return null
  return { id, key, ts: ts ?? 0, type, d: d || {}, note: note || '' }
}

export function mergeChrono(prev, fetched, max = 1500) {
  const fetchedKeys = new Map()
  for (const r of fetched) {
    if (r.key) fetchedKeys.set(r.key, (fetchedKeys.get(r.key) || 0) + 1)
  }
  const prevOnly = []
  for (const r of prev) {
    const n = r.key ? fetchedKeys.get(r.key) || 0 : 0
    if (n > 0) fetchedKeys.set(r.key, n - 1)
    else prevOnly.push(r)
  }
  const merged = [...fetched, ...prevOnly]
  merged.sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))
  return merged.length > max ? merged.slice(merged.length - max) : merged
}

export function filterByThread(items, threadId) {
  if (!threadId || threadId === 'all') return items
  return items.filter((it) => {
    if (it.kind === 'run' && it.runType === 'chat') return (it.threadId || 'main') === threadId
    return threadId === 'main'
  })
}

export function backgroundRunType(type, d) {
  if (d.task === 'quick_analysis' || d.source === 'quick' || type.startsWith('quick_')) return 'quick'
  if (type.startsWith('plan_') || d.source === 'plan') return 'plan'
  if (d.source === 'trigger' || d.trigger || d.trigger_rule || d.rule_id) return 'triggered'
  if (d.goal) return 'scheduled'
  return 'analysis'
}

function oneLine(s, max = 90) {
  const flat = String(s ?? '').replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

function firstLine(s, max = 90) {
  const line = String(s ?? '').split('\n').find((l) => l.trim()) || ''
  return oneLine(line, max)
}

export function stepObject(step) {
  const a = step.args && typeof step.args === 'object' ? step.args : {}
  let obj = ''
  switch (step.tool) {
    case 'sql': obj = firstLine(a.query); break
    case 'code': obj = firstLine(a.code || a.script); break
    case 'read_skill': obj = a.name || a.skill || a.skill_name || ''; break
    case 'analyze': obj = oneLine(a.goal); break
    case 'manage': obj = oneLine(a.instruction || a.request || a.goal || a.task); break
    case 'update_md': obj = a.file || ''; break
    case 'create_page': obj = a.title || a.page_id || a.name || ''; break
    case 'push_report': obj = a.title || oneLine(a.content, 60); break
    default: break
  }
  if (!obj) obj = oneLine(step.summary)
  return String(obj || '')
}

export function resultText(result, preview) {
  let r = result
  if (typeof r === 'string') {
    const trimmed = r.trim()
    if (trimmed.startsWith('{')) {
      try { r = JSON.parse(trimmed) } catch { }
    }
  }
  if (r && typeof r === 'object') {
    const inner = r.output ?? r.content ?? r.text ?? r.message ?? r.data
    if (inner !== undefined && inner !== null && inner !== '') {
      return typeof inner === 'string' ? inner : JSON.stringify(inner, null, 2)
    }
    if (r.error) return String(r.error)
    if (Object.keys(r).length === 0 && preview) return String(preview)
    if (r.columns && r.rows) return preview ? String(preview) : ''
    return JSON.stringify(r, null, 2)
  }
  if (r !== undefined && r !== null && r !== '') return String(r)
  return preview ? String(preview) : ''
}

export function formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null
  if (ms < 1000) return `${Math.max(0.1, Math.round(ms / 100) / 10).toFixed(1)}s`
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s - m * 60)}s`
}


export function buildTimeline(records, { now = Date.now(), idle = false } = {}) {
  const items = []
  const chatById = new Map()
  let implicitChat = null
  let openBg = null
  let lastChat = null
  let lastBg = null
  const idCounts = new Map()
  const uniqueId = (base) => {
    const n = idCounts.get(base) || 0
    idCounts.set(base, n + 1)
    return n ? `${base}#${n}` : base
  }

  const newRun = (init) => {
    const run = {
      kind: 'run',
      id: uniqueId(init.runId ? `run:chat:${init.runId}` : init.cycle != null && init.runType !== 'chat' ? `run:${init.runType}:c${init.cycle}` : `run:${init.runType}:${init.ts}`),
      runType: init.runType,
      runId: init.runId || null,
      threadId: null,
      startTs: init.ts,
      lastTs: init.ts,
      endTs: null,
      status: 'running',
      user: null,
      segments: [],
      stepIndex: [],
      goal: init.goal || '',
      cycle: init.cycle ?? null,
      report: null,
      reply: null,
      settled: false,
      quickState: null,
      tokens: null,
      warnings: [],
      closed: false,
      n: 0,
    }
    items.push(run)
    if (init.runType === 'chat') lastChat = run
    else lastBg = run
    return run
  }

  const touch = (run, ts) => {
    run.n += 1
    if (ts > run.lastTs) run.lastTs = ts
    if (run.settled && !run.closed) {
      run.settled = false
      run.status = 'running'
      run.endTs = null
    }
  }

  const close = (run, status, ts) => {
    if (!run || run.closed) return
    run.closed = true
    run.status = status
    run.endTs = ts
    if (run === openBg) openBg = null
    if (run === implicitChat) implicitChat = null
  }

  const chatRunFor = (rec, create = true) => {
    const run = chatRunForInner(rec, create)
    if (run && rec.d.thread_id && !run.threadId) run.threadId = rec.d.thread_id
    return run
  }

  const chatRunForInner = (rec, create = true) => {
    const rid = rec.d.run_id
    if (rid) {
      let run = chatById.get(rid)
      if (!run && create) {
        run = newRun({ runType: 'chat', runId: rid, ts: rec.ts })
        chatById.set(rid, run)
      }
      return run || null
    }
    if (implicitChat && !implicitChat.closed && rec.ts - implicitChat.lastTs <= GAP_CHAT_MS) return implicitChat
    if (!create) return null
    implicitChat = newRun({ runType: 'chat', ts: rec.ts })
    return implicitChat
  }

  const recentBg = (ts) => {
    if (openBg && !openBg.closed && ts - openBg.lastTs <= GAP_BG_MS) return openBg
    if (lastBg && lastBg.closed && lastBg.runType !== 'quick' && ts - (lastBg.endTs ?? lastBg.lastTs) <= BG_TAIL_MS) return lastBg
    return null
  }

  const bgRunFor = (rec, create = true) => {
    const recent = recentBg(rec.ts)
    if (recent) return recent
    if (!create) return null
    openBg = newRun({
      runType: backgroundRunType(rec.type, rec.d), ts: rec.ts, goal: rec.d.goal, cycle: rec.d.cycle,
    })
    return openBg
  }

  const stepsSegment = (run) => {
    const last = run.segments[run.segments.length - 1]
    if (last && last.kind === 'steps') return last
    const seg = { kind: 'steps', id: `${run.id}:s${run.segments.length}`, entries: [] }
    run.segments.push(seg)
    return seg
  }

  const addStep = (run, step) => {
    run.stepIndex.push(step)
    if (step.parent) {
      for (let i = run.stepIndex.length - 2; i >= 0; i--) {
        const cand = run.stepIndex[i]
        if (!cand.parent && cand.tool === step.parent) {
          cand.children.push(step)
          return
        }
      }
    }
    if (HIDDEN_TOOLS.has(step.tool)) return
    stepsSegment(run).entries.push(step)
  }

  const findStep = (run, d) => {
    const idx = run.stepIndex
    if (d.call_id) {
      for (let i = idx.length - 1; i >= 0; i--) if (idx[i].callId === d.call_id) return idx[i]
    }
    for (let i = idx.length - 1; i >= 0; i--) {
      const s = idx[i]
      if (s.status === 'running' && s.tool === d.tool && (s.parent || null) === (d.parent || null)) return s
    }
    return null
  }

  const addDraft = (run, { text, reason, detail, ts }) => {
    const seg = stepsSegment(run)
    for (let i = seg.entries.length - 1; i >= 0; i--) {
      const e = seg.entries[i]
      if (e.kind !== 'draft') continue
      if (ts - e.ts > DRAFT_MERGE_MS) break
      if (text && text.length > e.text.length) e.text = text
      if (reason && (!e.reason || e.reason === 'verification')) e.reason = reason
      if (detail && !e.detail) e.detail = detail
      return
    }
    if (!text && !reason) return
    seg.entries.push({ kind: 'draft', id: `${run.id}:d${seg.entries.length}`, text: text || '', reason: reason || '', detail: detail || '', ts })
  }

  const addNotice = (rec, tone) => {
    items.push({ kind: 'notice', id: uniqueId(`notice:${rec.type}:${rec.ts}`), ts: rec.ts, tone, type: rec.type, text: rec.note || rec.type })
  }

  for (const rec of records) {
    const { type, d, ts } = rec
    const toolMatch = TOOL_EVENT_RE.exec(type)

    if (toolMatch) {
      const isChat = toolMatch[1] === 'chat'
      const run = isChat ? chatRunFor(rec) : bgRunFor(rec)
      touch(run, ts)
      if (toolMatch[2] === 'call') {
        addStep(run, {
          kind: 'step',
          id: `${run.id}:t${run.stepIndex.length}`,
          callId: d.call_id || null,
          tool: d.tool || '',
          parent: d.parent || null,
          args: d.arguments || {},
          summary: d.summary || '',
          status: 'running',
          startTs: ts,
          endTs: null,
          result: undefined,
          preview: '',
          children: [],
        })
      } else {
        let step = findStep(run, d)
        if (!step) {
          step = {
            kind: 'step', id: `${run.id}:t${run.stepIndex.length}`, callId: d.call_id || null, tool: d.tool || '',
            parent: d.parent || null, args: {}, summary: '', status: 'running', startTs: ts, endTs: null,
            result: undefined, preview: '', children: [],
          }
          addStep(run, step)
        }
        step.status = d.status === 'error' || d.success === false ? 'error' : 'ok'
        step.endTs = ts
        step.result = d.result
        step.preview = d.result_preview || ''
      }
      continue
    }

    switch (type) {
      case 'user_message': {
        let run
        if (d.run_id) {
          run = chatRunFor(rec)
        } else {
          implicitChat = newRun({ runType: 'chat', ts })
          run = implicitChat
        }
        run.user = { content: d.content || '', sender: d.sender || '', channel: d.channel || '', ts }
        touch(run, ts)
        break
      }
      case 'thought': {
        const run = d.scope === 'chat' ? chatRunFor(rec) : bgRunFor(rec)
        stepsSegment(run).entries.push({ kind: 'thought', id: `${run.id}:th${run.n}`, text: d.content || '', ts })
        touch(run, ts)
        break
      }
      case 'chat_reply': {
        if (!d.content) break
        const bg = d.run_id ? null : recentBg(ts)
        if (bg && !bg.reply && !(implicitChat && !implicitChat.closed && implicitChat.startTs >= bg.startTs)) {
          bg.reply = { content: d.content, hash: d.message_hash || null, reportId: d.report_id || null, ts }
          touch(bg, ts)
          break
        }
        const run = chatRunFor(rec)
        run.segments.push({
          kind: 'reply', id: `${run.id}:r${run.segments.length}`, content: d.content,
          hash: d.message_hash || null, reportId: d.report_id || null, auto: !!d.auto, ts,
        })
        touch(run, ts)
        if (d.final !== false && !run.closed) {
          run.settled = true
          run.status = 'done'
          run.endTs = ts
        }
        break
      }
      case 'chat_verification': {
        if (d.tool && d.tool !== 'reply_user') break
        if (!HELD_VERDICTS.has(d.status)) break
        const run = chatRunFor(rec)
        addDraft(run, { text: d.preview || '', reason: 'verification', detail: d.detail || '', ts })
        touch(run, ts)
        break
      }
      case 'draft_held': {
        const run = chatRunFor(rec)
        addDraft(run, { text: d.content || '', reason: d.reason || '', detail: d.detail || '', ts })
        touch(run, ts)
        break
      }
      case 'chat_image': {
        const run = chatRunFor(rec)
        run.segments.push({
          kind: 'image', id: `${run.id}:i${run.segments.length}`, caption: d.caption || '', url: d.url || null,
          imageId: d.image_id || null, ts,
        })
        touch(run, ts)
        break
      }
      case 'chat_stopped': {
        const run = chatRunFor(rec, false) || lastChat
        if (run) {
          touch(run, ts)
          close(run, 'stopped', ts)
        }
        break
      }
      case 'chat_cleared':
        implicitChat = null
        addNotice(rec, 'info')
        break
      case 'token_usage': {
        const run = d.chat_id ? (chatById.get(d.run_id) || lastChat) : (openBg || lastBg)
        if (!run || run.closed) break
        const t = run.tokens || { prompt: 0, response: 0, thoughts: 0 }
        t.prompt += d.prompt_tokens ?? 0
        t.response += d.response_tokens ?? d.completion_tokens ?? 0
        t.thoughts += d.thoughts_tokens ?? 0
        run.tokens = t
        touch(run, ts)
        break
      }
      case 'cycle_start': {
        if (openBg && !openBg.closed) close(openBg, 'interrupted', ts)
        openBg = newRun({ runType: backgroundRunType(type, d), ts, goal: d.goal, cycle: d.cycle })
        touch(openBg, ts)
        break
      }
      case 'quick_analysis_start': {
        if (openBg && !openBg.closed) close(openBg, 'interrupted', ts)
        openBg = newRun({ runType: 'quick', ts })
        touch(openBg, ts)
        break
      }
      case 'quick_analysis_complete': {
        const run = bgRunFor(rec, false)
        if (run) {
          run.quickState = d.state || null
          touch(run, ts)
          close(run, 'done', ts)
        }
        break
      }
      case 'cycle_end': {
        const run = bgRunFor(rec, false)
        if (run) {
          touch(run, ts)
          close(run, 'done', ts)
        }
        break
      }
      case 'report_pushed': {
        const run = openBg || (lastBg && ts - lastBg.lastTs <= 2 * 60 * 1000 ? lastBg : null)
        if (run) {
          run.report = { id: d.report_id ?? null, ts }
          touch(run, ts)
        } else {
          items.push({ kind: 'notice', id: uniqueId(`notice:report:${ts}`), ts, tone: 'ok', type, text: rec.note, reportId: d.report_id ?? null })
        }
        break
      }
      case 'agent_stopped':
        for (const it of items) if (it.kind === 'run' && !it.closed) close(it, 'stopped', ts)
        addNotice(rec, 'info')
        break
      case 'agent_error':
        for (const it of items) if (it.kind === 'run' && !it.closed) close(it, 'error', ts)
        addNotice(rec, 'bad')
        break
      case 'agent_started':
      case 'agent_waiting':
      case 'analysis_deferred':
      case 'analysis_released':
        addNotice(rec, 'info')
        break
      case 'startup_error':
        addNotice(rec, 'bad')
        break
      case 'error':
      case 'forced_sleep':
      case 'token_truncated': {
        const run = d.chat_id ? (chatById.get(d.run_id) || lastChat) : openBg
        if (run && !run.closed) {
          run.warnings.push({ type, text: rec.note, ts })
          touch(run, ts)
        } else {
          addNotice(rec, type === 'error' ? 'bad' : 'warn')
        }
        break
      }
      default:
        break
    }
  }

  for (const it of items) {
    if (it.kind !== 'run') continue
    if (it.status === 'running') {
      if (idle && now - it.lastTs > IDLE_GRACE_MS) it.status = 'done'
      else if (now - it.lastTs > STALE_MS) it.status = 'interrupted'
    }
    if (it.status !== 'running' && !it.endTs) it.endTs = it.lastTs
    const dangling = it.status === 'stopped' ? 'stopped' : 'interrupted'
    let errors = 0
    let visible = 0
    for (const s of it.stepIndex) {
      if (s.status === 'running' && it.status !== 'running') {
        s.status = it.status === 'done' ? 'interrupted' : dangling
      }
      if (s.status === 'error') errors += 1
      if (!HIDDEN_TOOLS.has(s.tool)) visible += 1
    }
    const delivered = new Set(it.segments.filter((x) => x.kind === 'reply').map((x) => x.content.replace(/\s+/g, ' ').trim()))
    for (const seg of it.segments) {
      if (seg.kind !== 'steps') continue
      seg.entries = seg.entries.filter((e) => e.kind !== 'draft' || !e.text || !delivered.has(e.text.replace(/\s+/g, ' ').trim()))
    }
    it.segments = it.segments.filter((x) => x.kind !== 'steps' || x.entries.length > 0)
    it.stepCount = visible
    it.hiccups = errors + it.warnings.length
    it.sig = `${it.n}:${it.status}:${it.stepIndex.length}:${it.threadId || ''}`
  }

  return items
}

export function findLiveRun(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'run' && it.status === 'running') return it
  }
  return null
}

export function currentStep(run) {
  if (!run) return null
  for (let i = run.stepIndex.length - 1; i >= 0; i--) {
    const s = run.stepIndex[i]
    if (s.status === 'running' && !HIDDEN_TOOLS.has(s.tool)) return s
  }
  return null
}
