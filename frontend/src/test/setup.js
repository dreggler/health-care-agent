import i18n from '../i18n'
i18n.changeLanguage('en')

import '@testing-library/jest-dom'


class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  constructor(url) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    this.onopen = null
    this.onmessage = null
    this.onerror = null
    this.onclose = null

    Promise.resolve().then(() => {
      this.readyState = MockWebSocket.OPEN
      if (this.onopen) this.onopen(new Event('open'))
    })
  }

  send(_data) {
  }

  close() {
    this.readyState = MockWebSocket.CLOSED
    if (this.onclose) this.onclose(new CloseEvent('close'))
  }
}

if (typeof globalThis.WebSocket === 'undefined') {
  globalThis.WebSocket = MockWebSocket
}


globalThis.alert = vi.fn()
globalThis.confirm = vi.fn(() => true)


Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})


globalThis.ResizeObserver = class ResizeObserver {
  constructor(cb) { this._cb = cb }
  observe() {}
  unobserve() {}
  disconnect() {}
}


const localStorageStore = {}
Object.defineProperty(window, 'localStorage', {
  value: {
    getItem: vi.fn((key) => localStorageStore[key] ?? null),
    setItem: vi.fn((key, val) => { localStorageStore[key] = String(val) }),
    removeItem: vi.fn((key) => { delete localStorageStore[key] }),
    clear: vi.fn(() => { Object.keys(localStorageStore).forEach((k) => delete localStorageStore[k]) }),
  },
})


const origError = console.error
console.error = (...args) => {
  if (typeof args[0] === 'string' && args[0].includes('act(')) return
  origError(...args)
}
