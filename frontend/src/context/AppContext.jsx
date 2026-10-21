import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from 'react'
import { api } from '../lib/api'
import { parseBackendDate } from '../lib/utils'


const initialState = {
  featureTypes: [],
  agentStatus: null,
  globalError: null,
  loading: true,
  streaming: {
    isStreaming: false,
    streamData: null,
    historicalData: [],
    liveHistoryWindow: '1hour',
    streamError: null,
  },
}


const HISTORY_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
const HISTORY_MAX_RECORDS = 100000
const HISTORY_TARGET_RECORDS = 80000

function _trimHistorical(records) {
  if (records.length <= HISTORY_MAX_RECORDS) return records
  const tsOf = (r) => (r && r.date ? parseBackendDate(r.date).getTime() : NaN)
  let maxTs = null
  for (const r of records) {
    const ts = tsOf(r)
    if (!isNaN(ts) && (maxTs === null || ts > maxTs)) maxTs = ts
  }
  let trimmed = records
  if (maxTs !== null) {
    const cutoff = maxTs - HISTORY_RETENTION_MS
    trimmed = records.filter((r) => {
      const ts = tsOf(r)
      return isNaN(ts) || ts >= cutoff
    })
  }
  return trimmed.length > HISTORY_TARGET_RECORDS
    ? trimmed.slice(trimmed.length - HISTORY_TARGET_RECORDS)
    : trimmed
}


function reducer(state, action) {
  switch (action.type) {
    case 'SET_FEATURE_TYPES':
      return { ...state, featureTypes: action.payload }
    case 'SET_AGENT_STATUS':
      return { ...state, agentStatus: action.payload }
    case 'SET_GLOBAL_ERROR':
      return { ...state, globalError: action.payload }
    case 'SET_LOADING':
      return { ...state, loading: action.payload }
    case 'SET_STREAMING':
      return { ...state, streaming: { ...state.streaming, ...action.payload } }
    case 'STREAM_BATCH': {
      const { batch, records } = action.payload
      const prev = state.streaming.historicalData
      return {
        ...state,
        streaming: {
          ...state.streaming,
          streamData: batch,
          historicalData: records.length ? _trimHistorical(prev.concat(records)) : prev,
        },
      }
    }
    case 'APPEND_HISTORICAL':
      return {
        ...state,
        streaming: {
          ...state.streaming,
          historicalData: _trimHistorical(state.streaming.historicalData.concat(action.payload)),
        },
      }
    default:
      return state
  }
}


const AppContext = createContext(null)
const AppActionsContext = createContext(null)

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const wsRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const reconnectAttemptRef = useRef(0)
  const lastWindowRef = useRef('1hour')
  const stoppedRef = useRef(false)
  const startStreamRef = useRef(null)
  const streamSeqRef = useRef(0)
  const unmountedRef = useRef(false)


  const _scheduleReconnect = useCallback(() => {
    if (stoppedRef.current) return
    if (reconnectTimerRef.current) return
    const attempt = reconnectAttemptRef.current
    const delay = Math.min(2000 * 2 ** attempt, 30000)
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null
      reconnectAttemptRef.current = attempt + 1
      startStreamRef.current?.(lastWindowRef.current)
    }, delay)
  }, [])

  const startStream = useCallback(async (liveHistoryWindow = '1hour') => {
    const seq = ++streamSeqRef.current
    const superseded = () => seq !== streamSeqRef.current || unmountedRef.current
    try {
      stoppedRef.current = false
      lastWindowRef.current = liveHistoryWindow

      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }

      if (wsRef.current) {
        const old = wsRef.current
        wsRef.current = null
        try { old.close() } catch (err) { console.error('stream close failed:', err) }
      }

      dispatch({
        type: 'SET_STREAMING',
        payload: {
          isStreaming: false,
          streamData: null,
          historicalData: [],
          liveHistoryWindow,
        },
      })

      await api.setStreamConfig(true, '1month')
      if (superseded() || stoppedRef.current) return

      const websocket = api.connectDataStream()

      websocket.onopen = () => {
        if (wsRef.current !== websocket) return
        reconnectAttemptRef.current = 0
        dispatch({ type: 'SET_STREAMING', payload: { isStreaming: true, streamError: null } })
      }

      websocket.onmessage = (event) => {
        if (wsRef.current !== websocket) return
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'data_batch' && data.batch) {
            dispatch({
              type: 'STREAM_BATCH',
              payload: {
                batch: data.batch,
                records: Array.isArray(data.batch.data) ? data.batch.data : [],
              },
            })
          } else if (data.type === 'error') {
            console.error(`Stream error: ${data.error}`)
            dispatch({
              type: 'SET_STREAMING',
              payload: { isStreaming: false, streamError: String(data.error || 'Stream error') },
            })
            try { websocket.close() } catch (err) { console.error('stream close failed:', err) }
          }
        } catch (err) {
          console.error('Failed to parse stream message:', err)
        }
      }

      websocket.onerror = () => {
        if (wsRef.current !== websocket) return
        dispatch({ type: 'SET_STREAMING', payload: { isStreaming: false } })
      }

      websocket.onclose = () => {
        if (wsRef.current !== websocket) return
        wsRef.current = null
        dispatch({ type: 'SET_STREAMING', payload: { isStreaming: false } })
        _scheduleReconnect()
      }

      wsRef.current = websocket
    } catch (err) {
      if (superseded()) return
      dispatch({
        type: 'SET_STREAMING',
        payload: { isStreaming: false, streamError: err?.message || 'Failed to start stream' },
      })
      console.error('Failed to start stream:', err?.message || 'Unknown error')
      _scheduleReconnect()
    }
  }, [_scheduleReconnect])

  useEffect(() => {
    startStreamRef.current = startStream
  }, [startStream])

  const stopStream = useCallback(() => {
    stoppedRef.current = true
    streamSeqRef.current += 1
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
    if (wsRef.current) {
      const old = wsRef.current
      wsRef.current = null
      try {
        old.close()
      } catch (err) {
        console.error('stream close failed:', err)
      }
    }
    dispatch({ type: 'SET_STREAMING', payload: { isStreaming: false } })
    Promise.resolve(api.setStreamConfig(false)).catch(err => console.error('stream config reset failed:', err))
  }, [])

  const reconnectStream = useCallback((newWindow) => {
    dispatch({ type: 'SET_STREAMING', payload: { liveHistoryWindow: newWindow } })
    api.setStreamConfig(null, newWindow).catch(() => {})
  }, [])

  const updateStreamingConfig = useCallback((updates) => {
    dispatch({ type: 'SET_STREAMING', payload: updates })
  }, [])

  const refreshAgentStatus = useCallback(async () => {
    try {
      const res = await api.getAgentStatus()
      if (res?.success) {
        const agents = res.agents || {}
        const firstPid = Object.keys(agents)[0]
        if (firstPid) {
          dispatch({ type: 'SET_AGENT_STATUS', payload: { ...agents[firstPid], running: true, user_id: firstPid } })
        } else {
          dispatch({ type: 'SET_AGENT_STATUS', payload: { running: false } })
        }
      }
    } catch (_) {
    }
  }, [])


  const bootstrap = useCallback(async (isCancelled = () => false) => {
    dispatch({ type: 'SET_LOADING', payload: true })
    dispatch({ type: 'SET_GLOBAL_ERROR', payload: null })
    try {
      const streamCfg = await api.getStreamConfig()
      const savedWindow = streamCfg?.live_history_window || '1hour'

      const [featureErr] = await Promise.all([
        _loadFeatureTypes(dispatch),
        refreshAgentStatus().catch(() => {}),
      ])

      if (isCancelled()) return
      const failure = (streamCfg && streamCfg.success === false && streamCfg.error) || featureErr
      if (failure) dispatch({ type: 'SET_GLOBAL_ERROR', payload: String(failure) })

      console.log(`Auto-starting stream with window: ${savedWindow}`)
      startStream(savedWindow)
    } catch (err) {
      if (!isCancelled()) dispatch({ type: 'SET_GLOBAL_ERROR', payload: String(err) })
    } finally {
      if (!isCancelled()) dispatch({ type: 'SET_LOADING', payload: false })
    }
  }, [refreshAgentStatus, startStream])

  useEffect(() => {
    unmountedRef.current = false
    let cancelled = false
    bootstrap(() => cancelled)
    return () => {
      cancelled = true
      unmountedRef.current = true
      stoppedRef.current = true
      streamSeqRef.current += 1
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (wsRef.current) {
        const old = wsRef.current
        wsRef.current = null
        try { old.close() } catch (err) { console.error('stream close failed:', err) }
      }
    }
  }, [bootstrap])

  const reload = useCallback(() => bootstrap(), [bootstrap])

  const actions = useMemo(() => ({
    refreshAgentStatus,
    startStream,
    stopStream,
    reconnectStream,
    updateStreamingConfig,
    reload,
    dispatch,
  }), [refreshAgentStatus, startStream, stopStream, reconnectStream, updateStreamingConfig, reload])

  const value = useMemo(() => ({ ...state, ...actions }), [state, actions])

  return (
    <AppActionsContext.Provider value={actions}>
      <AppContext.Provider value={value}>{children}</AppContext.Provider>
    </AppActionsContext.Provider>
  )
}


export function useApp() {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp must be used inside <AppProvider>')
  return ctx
}

export function useAppActions() {
  const ctx = useContext(AppActionsContext)
  if (!ctx) throw new Error('useAppActions must be used inside <AppProvider>')
  return ctx
}


async function _loadFeatureTypes(dispatch) {
  try {
    const res = await api.getFeatureTypes()
    if (res?.success && Array.isArray(res.feature_types)) {
      dispatch({ type: 'SET_FEATURE_TYPES', payload: res.feature_types })
      return ''
    }
    return (res && res.success === false && res.error) || ''
  } catch (err) {
    return err?.message || ''
  }
}
