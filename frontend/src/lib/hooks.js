import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'

function subscribeVisibility(cb) {
  document.addEventListener('visibilitychange', cb)
  return () => document.removeEventListener('visibilitychange', cb)
}

export function useDocumentVisible() {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== 'hidden',
    () => true,
  )
}

export function usePolling(fn, ms, enabled = true) {
  const visible = useDocumentVisible()
  const fnRef = useRef(fn)
  useEffect(() => {
    fnRef.current = fn
  }, [fn])
  const wasPausedRef = useRef(false)

  useEffect(() => {
    if (!enabled || !visible) {
      wasPausedRef.current = true
      return undefined
    }
    if (wasPausedRef.current) {
      wasPausedRef.current = false
      fnRef.current()
    }
    const id = setInterval(() => fnRef.current(), ms)
    return () => clearInterval(id)
  }, [ms, enabled, visible])
}

export function useOnActivate(active, fn) {
  const fnRef = useRef(fn)
  useEffect(() => {
    fnRef.current = fn
  }, [fn])
  const prevRef = useRef(active)
  useEffect(() => {
    if (active && !prevRef.current) fnRef.current()
    prevRef.current = active
  }, [active])
}

export function useFlash(ms = 5000) {
  const [flash, setFlashState] = useState(null)
  const timerRef = useRef(null)
  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])
  const setFlash = useCallback((type, text) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    if (!type) {
      setFlashState(null)
      return
    }
    setFlashState({ type, text })
    if (ms > 0) {
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        setFlashState(null)
      }, ms)
    }
  }, [ms])
  return [flash, setFlash]
}
