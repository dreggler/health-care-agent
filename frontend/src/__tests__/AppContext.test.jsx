import { render, screen, act, waitFor } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { vi, describe, it, expect, beforeEach } from 'vitest'

vi.mock('../lib/api', () => import('../test/mocks/api'))

import { AppProvider, useApp } from '../context/AppContext'
import { api } from '../test/mocks/api'


function renderAppHook() {
  const wrapper = ({ children }) => <AppProvider>{children}</AppProvider>
  return renderHook(() => useApp(), { wrapper })
}


describe('AppContext', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getStreamConfig.mockResolvedValue({
      success: true,
      is_streaming: false,
      live_history_window: '1hour',
    })
    api.getFeatureTypes.mockResolvedValue({
      success: true,
      feature_types: ['Heart Rate', 'Steps'],
    })
    api.getAgentStatus.mockResolvedValue({
      success: true,
      agents: {},
    })
  })


  it('provides correct initial state', async () => {
    const { result } = renderAppHook()

    await waitFor(() => {
      expect(result.current.loading).toBe(false)
    })

    expect(result.current.featureTypes).toEqual(['Heart Rate', 'Steps'])
    expect(result.current.agentStatus).toEqual({ running: false })
    expect(result.current.globalError).toBeNull()
    expect(result.current.streaming.isStreaming).toBe(false)
    expect(result.current.streaming.historicalData).toEqual([])
    expect(result.current.streaming.liveHistoryWindow).toBe('1hour')
  })


  it('throws when useApp is used outside AppProvider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => {
      renderHook(() => useApp())
    }).toThrow('useApp must be used inside <AppProvider>')
    spy.mockRestore()
  })


  it('SET_FEATURE_TYPES updates featureTypes', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.dispatch({
        type: 'SET_FEATURE_TYPES',
        payload: ['Blood Oxygen', 'Respiratory Rate'],
      })
    })

    expect(result.current.featureTypes).toEqual(['Blood Oxygen', 'Respiratory Rate'])
  })


  it('SET_AGENT_STATUS updates agentStatus', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.dispatch({
        type: 'SET_AGENT_STATUS',
        payload: { running: true, user_id: 'LiveUser', status: 'active' },
      })
    })

    expect(result.current.agentStatus).toEqual({
      running: true,
      user_id: 'LiveUser',
      status: 'active',
    })
  })


  it('SET_STREAMING merges partial updates without losing existing keys', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.dispatch({
        type: 'SET_STREAMING',
        payload: { isStreaming: true },
      })
    })

    expect(result.current.streaming.isStreaming).toBe(true)
    expect(result.current.streaming.liveHistoryWindow).toBe('1hour')
    expect(Array.isArray(result.current.streaming.historicalData)).toBe(true)
  })


  it('APPEND_HISTORICAL appends data to historicalData', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    const batch1 = [
      { timestamp: '2026-03-20T10:00:00Z', feature_type: 'Heart Rate', value: 72 },
      { timestamp: '2026-03-20T10:01:00Z', feature_type: 'Heart Rate', value: 74 },
    ]
    const batch2 = [
      { timestamp: '2026-03-20T10:02:00Z', feature_type: 'Steps', value: 150 },
    ]

    act(() => {
      result.current.dispatch({ type: 'APPEND_HISTORICAL', payload: batch1 })
    })
    expect(result.current.streaming.historicalData).toHaveLength(2)

    act(() => {
      result.current.dispatch({ type: 'APPEND_HISTORICAL', payload: batch2 })
    })
    expect(result.current.streaming.historicalData).toHaveLength(3)
    expect(result.current.streaming.historicalData[2].feature_type).toBe('Steps')
  })


  it('SET_GLOBAL_ERROR sets and clears the global error', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.dispatch({ type: 'SET_GLOBAL_ERROR', payload: 'Network timeout' })
    })
    expect(result.current.globalError).toBe('Network timeout')

    act(() => {
      result.current.dispatch({ type: 'SET_GLOBAL_ERROR', payload: null })
    })
    expect(result.current.globalError).toBeNull()
  })


  it('unknown action type does not mutate state', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    const before = result.current.featureTypes

    act(() => {
      result.current.dispatch({ type: 'NONEXISTENT_ACTION', payload: 'hello' })
    })

    expect(result.current.featureTypes).toBe(before)
  })


  it('startStream calls setStreamConfig and connectDataStream', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    await act(async () => {
      await result.current.startStream('1day')
    })

    expect(api.setStreamConfig).toHaveBeenCalledWith(true, '1month')
    expect(api.connectDataStream).toHaveBeenCalled()
  })


  it('stopStream marks streaming as false and calls setStreamConfig(false)', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    await act(async () => {
      await result.current.startStream('1hour')
    })

    act(() => {
      result.current.stopStream()
    })

    expect(result.current.streaming.isStreaming).toBe(false)
    expect(api.setStreamConfig).toHaveBeenCalledWith(false)
  })


  it('refreshAgentStatus sets running=true when an agent exists', async () => {
    api.getAgentStatus.mockResolvedValueOnce({
      success: true,
      agents: {
        LiveUser: { status: 'running', cycles_completed: 5 },
      },
    })

    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    api.getAgentStatus.mockResolvedValueOnce({
      success: true,
      agents: {
        LiveUser: { status: 'running', cycles_completed: 10 },
      },
    })

    await act(async () => {
      await result.current.refreshAgentStatus()
    })

    expect(result.current.agentStatus.running).toBe(true)
    expect(result.current.agentStatus.user_id).toBe('LiveUser')
  })


  it('refreshAgentStatus keeps the previous status when the request fails', async () => {
    api.getAgentStatus.mockResolvedValueOnce({
      success: true,
      agents: { LiveUser: { status: 'running' } },
    })
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.agentStatus.running).toBe(true)

    api.getAgentStatus.mockRejectedValueOnce(new Error('Server down'))
    await act(async () => {
      await result.current.refreshAgentStatus()
    })
    expect(result.current.agentStatus.running).toBe(true)

    api.getAgentStatus.mockResolvedValueOnce({ success: false, error: 'HTTP 502' })
    await act(async () => {
      await result.current.refreshAgentStatus()
    })
    expect(result.current.agentStatus.running).toBe(true)
  })


  it('loads feature types during bootstrap', async () => {
    api.getFeatureTypes.mockResolvedValueOnce({
      success: true,
      feature_types: ['Sleep Analysis', 'Exercise Time'],
    })

    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.featureTypes).toEqual(['Sleep Analysis', 'Exercise Time'])
    expect(api.getFeatureTypes).toHaveBeenCalled()
  })


  it('loads stream config during bootstrap', async () => {
    api.getStreamConfig.mockResolvedValueOnce({
      success: true,
      is_streaming: false,
      live_history_window: '1week',
    })

    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.streaming.liveHistoryWindow).toBe('1week')
    expect(api.getStreamConfig).toHaveBeenCalled()
  })


  it('updateStreamingConfig dispatches partial streaming updates', async () => {
    const { result } = renderAppHook()

    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.updateStreamingConfig({ liveHistoryWindow: '1month' })
    })

    expect(result.current.streaming.liveHistoryWindow).toBe('1month')
    expect(result.current.streaming.isStreaming).toBe(false)
  })
})
