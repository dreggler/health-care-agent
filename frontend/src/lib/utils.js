const ensureUTC = (ts) => {
  if (typeof ts === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(ts)) {
    return ts.replace(' ', 'T') + 'Z'
  }
  return ts
}

export const parseBackendDate = (ts) => new Date(ensureUTC(ts))

export const formatFullDateTime = (ts) => {
  if (!ts) return '--'
  const d = new Date(ensureUTC(ts))
  if (isNaN(d.getTime())) return String(ts)
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hours = String(d.getHours()).padStart(2, '0')
  const minutes = String(d.getMinutes()).padStart(2, '0')
  const seconds = String(d.getSeconds()).padStart(2, '0')
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
}
