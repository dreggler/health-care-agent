import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, getAuthToken } from '../lib/api'
import {
  AppWindow, Trash2, ExternalLink, RefreshCw, X, Maximize2, Minimize2, AlertTriangle,
} from 'lucide-react'
import { parseBackendDate } from '../lib/utils'
import { useFlash, useOnActivate } from '../lib/hooks'
import InlineFlash from '../components/InlineFlash'
import Skeleton from '../components/Skeleton'


const MAX_REQUEST_ID_LEN = 64
const MAX_PARAM_COUNT = 8
const MAX_PARAM_KEY_LEN = 32
const MAX_PARAM_VALUE_LEN = 256
const MAX_BODY_BYTES = 64 * 1024
const PARAM_KEY_RE = /^[A-Za-z0-9_-]{1,32}$/

function sanitiseParams(raw) {
  if (raw === null || raw === undefined) return {}
  if (typeof raw !== 'object' || Array.isArray(raw)) return null
  const keys = Object.keys(raw)
  if (keys.length > MAX_PARAM_COUNT) return null
  const out = {}
  for (const k of keys) {
    if (k.length > MAX_PARAM_KEY_LEN || !PARAM_KEY_RE.test(k)) return null
    const v = raw[k]
    if (v === null || v === undefined) continue
    if (typeof v === 'object') return null
    const s = String(v)
    if (s.length > MAX_PARAM_VALUE_LEN) return null
    out[k] = s
  }
  return out
}

function sanitiseBody(raw) {
  const value = raw === null || raw === undefined ? {} : raw
  if (typeof value !== 'object' || Array.isArray(value)) return null
  let json
  try {
    json = JSON.stringify(value)
  } catch {
    return null
  }
  if (json === undefined || json.length > MAX_BODY_BYTES) return null
  return json
}

export default function PersonalisedPages({ active = true }) {
  const { t } = useTranslation()
  const [pages, setPages] = useState([])
  const [loading, setLoading] = useState(true)
  const [activePage, setActivePage] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const [deleting, setDeleting] = useState(null)
  const [pageDoc, setPageDoc] = useState(null)
  const iframeRef = useRef(null)
  const viewerRef = useRef(null)
  const [loadError, setLoadError] = useState('')
  const [flash, setFlash] = useFlash(6000)

  const loadPages = async (silent = false) => {
    if (silent !== true) setLoading(true)
    const res = await api.listPersonalisedPages()
    if (res.success) {
      setPages(res.pages || [])
      setLoadError('')
    } else {
      setLoadError(res.error || t('common.load_failed'))
    }
    setLoading(false)
  }

  useOnActivate(active, () => loadPages(true))

  useEffect(() => {
    if (!expanded) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setExpanded(false) }
    document.addEventListener('keydown', onKey)
    viewerRef.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [expanded])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await api.listPersonalisedPages()
      if (cancelled) return
      const list = res.success ? (res.pages || []) : []
      if (!res.success) setLoadError(res.error || t('common.load_failed'))
      setPages(list)
      const wanted = new URLSearchParams(window.location.search).get('page')
      const match = wanted ? list.find((p) => p.page_id === wanted) : null
      if (match) {
        setActivePage(match)
        setExpanded(true)
      }
      setLoading(false)
    })()
    return () => { cancelled = true }
  }, [])

  const activePageIdForDoc = activePage?.page_id
  useEffect(() => {
    if (!activePageIdForDoc) return undefined
    let cancelled = false
    ;(async () => {
      const res = await api.getPersonalisedPageHtml(activePageIdForDoc)
      if (cancelled) return
      setPageDoc(
        res.success
          ? { pageId: activePageIdForDoc, html: res.html }
          : { pageId: activePageIdForDoc, error: res.error || '' },
      )
    })()
    return () => { cancelled = true }
  }, [activePageIdForDoc])

  const activePageId = activePage?.page_id
  useEffect(() => {
    if (!activePageId) return undefined

    const reply = (target, requestId, msg) => {
      if (!iframeRef.current || iframeRef.current.contentWindow !== target) return
      target.postMessage({ ...msg, type: 'hime:data-response', requestId }, '*')
    }

    const onMessage = async (event) => {
      const frame = iframeRef.current
      if (!frame || !event.source || event.source !== frame.contentWindow) return

      const msg = event.data
      if (!msg || typeof msg !== 'object' || msg.type !== 'hime:data-request') return

      const requestId = msg.requestId
      if (typeof requestId !== 'string' || !requestId || requestId.length > MAX_REQUEST_ID_LEN) return

      const src = event.source

      const method = msg.method === 'POST' ? 'POST' : msg.method === 'GET' ? 'GET' : null
      if (!method) {
        reply(src, requestId, { ok: false, error: 'Unsupported method' })
        return
      }

      if (msg.pageId !== undefined && msg.pageId !== null && msg.pageId !== activePageId) {
        reply(src, requestId, { ok: false, error: 'Page mismatch' })
        return
      }
      const url = `/api/personalised-pages/${encodeURIComponent(activePageId)}/data`

      const token = getAuthToken()
      const headers = token ? { Authorization: `Bearer ${token}` } : {}

      let request
      if (method === 'POST') {
        const body = sanitiseBody(msg.body)
        if (body === null) {
          reply(src, requestId, { ok: false, error: 'Invalid request body' })
          return
        }
        request = [url, { method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body }]
      } else {
        const params = sanitiseParams(msg.params)
        if (params === null) {
          reply(src, requestId, { ok: false, error: 'Invalid query parameters' })
          return
        }
        const qs = new URLSearchParams(params).toString()
        request = [qs ? `${url}?${qs}` : url, { method: 'GET', headers }]
      }

      try {
        const res = await fetch(...request)
        const payload = await res.json().catch(() => null)
        if (!res.ok) {
          reply(src, requestId, {
            ok: false,
            status: res.status,
            error: payload?.detail || payload?.error || `HTTP ${res.status}`,
          })
          return
        }
        reply(src, requestId, { ok: true, status: res.status, payload })
      } catch (err) {
        reply(src, requestId, { ok: false, error: err?.message || 'Network error' })
      }
    }

    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [activePageId])

  const handleDelete = async (pageId) => {
    if (!confirm(t('pages.confirm_delete', { pageId }))) return
    setDeleting(pageId)
    try {
      const res = await api.deletePersonalisedPage(pageId)
      if (res.success) {
        setPages((prev) => prev.filter((a) => a.page_id !== pageId))
        if (activePage?.page_id === pageId) setActivePage(null)
      } else {
        console.error('Delete failed:', res.error)
        setFlash('error', t('pages.delete_failed', { error: res.error || t('common.unknown_error') }))
      }
    } catch (err) {
      console.error('Delete page error:', err)
      setFlash('error', t('pages.delete_failed', { error: err.message || t('common.network_error') }))
    }
    setDeleting(null)
  }

  const openPage = (page) => {
    setActivePage(page)
    setExpanded(false)
  }

  const openInNewTab = (pageId) => {
    window.open(`/pages?page=${encodeURIComponent(pageId)}`, '_blank')
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-3">
          <AppWindow className="w-8 h-8 text-primary-600" />
          <h2 className="page-title">{t('pages.title')}</h2>
          <span className="text-sm text-ink-2 bg-sunken px-2 py-0.5 rounded-full">
            {pages.length}
          </span>
        </div>
        <button type="button" onClick={() => loadPages()} className="btn btn-secondary flex items-center space-x-2">
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          <span>{t('pages.refresh')}</span>
        </button>
      </div>

      <InlineFlash flash={flash} />
      {loadError && (
        <div role="alert" className="flex items-center gap-3 rounded-card border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad-ink">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" aria-hidden="true" />
          <span className="flex-1 break-words">{t('pages.list_failed', { error: loadError })}</span>
          <button type="button" onClick={() => loadPages()} className="font-semibold underline hover:text-bad-ink">
            {t('common.retry')}
          </button>
        </div>
      )}

      {loading && pages.length === 0 ? (
        <Skeleton lines={4} label={t('pages.loading')} />
      ) : pages.length === 0 ? (
        loadError ? null : <div className="card p-12 text-center">
          <AppWindow className="w-12 h-12 text-ink-3 mx-auto mb-3" />
          <p className="text-ink-2">{t('pages.empty')}</p>
          <p className="text-sm text-ink-3 mt-1">
            {t('pages.empty_hint')}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {pages.map((page) => (
            <div
              key={page.page_id}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget) return
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  openPage(page)
                }
              }}
              className={`card p-4 cursor-pointer transition-all hover:shadow-md focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 ${
                activePage?.page_id === page.page_id
                  ? 'ring-2 ring-primary-500 shadow-md'
                  : ''
              }`}
              onClick={() => openPage(page)}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-ink truncate">
                    {page.display_name}
                  </h3>
                  <p className="text-xs text-ink-3 font-mono mt-0.5">{page.page_id}</p>
                </div>
                <div className="flex items-center space-x-1 ml-2 flex-shrink-0">
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); openInNewTab(page.page_id) }}
                    className="p-1.5 text-ink-3 hover:text-primary-600 rounded-control hover:bg-sunken"
                    title={t('pages.open_new_tab')}
                    aria-label={t('pages.open_new_tab')}
                  >
                    <ExternalLink className="w-4 h-4" />
                  </button>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleDelete(page.page_id) }}
                    disabled={deleting === page.page_id}
                    className="p-1.5 text-ink-3 hover:text-bad-ink rounded-control hover:bg-bad/10 disabled:opacity-50"
                    title={t('pages.delete_page')}
                    aria-label={t('pages.delete_page')}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
              {page.description && (
                <p className="text-sm text-ink-2 mt-2 line-clamp-2">{page.description}</p>
              )}
              {page.created_at && (
                <p className="text-xs text-ink-3 mt-2">
                  {parseBackendDate(page.created_at).toLocaleString()}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {activePage && (
        <div
          ref={viewerRef}
          tabIndex={-1}
          role={expanded ? 'dialog' : undefined}
          aria-modal={expanded ? 'true' : undefined}
          aria-label={expanded ? activePage.display_name : undefined}
          className={`card overflow-hidden outline-none ${expanded ? 'fixed inset-4 z-50 m-0' : ''}`}
        >
          <div className="flex items-center justify-between px-4 py-2 bg-sunken border-b border-line">
            <div className="flex items-center space-x-2 min-w-0">
              <AppWindow className="w-4 h-4 text-primary-600 flex-shrink-0" />
              <span className="font-medium text-sm text-ink truncate">
                {activePage.display_name}
              </span>
            </div>
            <div className="flex items-center space-x-1">
              <button
                type="button"
                onClick={() => openInNewTab(activePage.page_id)}
                className="p-1.5 text-ink-3 hover:text-primary-600 rounded-chip"
                title={t('pages.open_new_tab')}
                aria-label={t('pages.open_new_tab')}
              >
                <ExternalLink className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={() => setExpanded(!expanded)}
                className="p-1.5 text-ink-3 hover:text-ink-2 rounded-chip"
                title={expanded ? t('pages.minimize') : t('pages.maximize')}
                aria-label={expanded ? t('pages.minimize') : t('pages.maximize')}
              >
                {expanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
              </button>
              <button
                type="button"
                onClick={() => { setActivePage(null); setExpanded(false) }}
                className="p-1.5 text-ink-3 hover:text-ink-2 rounded-chip"
                title={t('pages.close')}
                aria-label={t('pages.close')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>
          {pageDoc?.pageId === activePage.page_id && pageDoc.html ? (
            <iframe
              ref={iframeRef}
              key={activePage.page_id}
              srcDoc={pageDoc.html}
              className={`w-full border-0 ${expanded ? 'flex-1' : ''}`}
              style={{ height: expanded ? 'calc(100% - 41px)' : '600px' }}
              title={activePage.display_name}
              sandbox="allow-scripts"
            />
          ) : (
            <div
              className="flex items-center justify-center text-sm text-ink-2 px-4 text-center"
              style={{ height: expanded ? 'calc(100% - 41px)' : '600px' }}
            >
              {pageDoc?.pageId === activePage.page_id && pageDoc.error !== undefined
                ? t('pages.load_failed', { error: pageDoc.error || t('common.unknown_error') })
                : t('pages.loading_page')}
            </div>
          )}
        </div>
      )}

      {expanded && (
        <div
          className="fixed inset-0 bg-black/30 z-40"
          onClick={() => setExpanded(false)}
        />
      )}
    </div>
  )
}
