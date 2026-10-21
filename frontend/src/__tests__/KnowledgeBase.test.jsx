import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, describe, it, expect, beforeEach } from 'vitest'

vi.mock('../lib/api', () => import('../test/mocks/api'))

import {
  api,
  mockMemoryStats,
  mockTools,
  mockTableInspectData,
} from '../test/mocks/api'
import { AppProvider } from '../context/AppContext'
import { BrowserRouter } from 'react-router-dom'
import KnowledgeBase from '../pages/KnowledgeBase'

function renderKnowledgeBase() {
  return render(
    <BrowserRouter>
      <AppProvider>
        <KnowledgeBase />
      </AppProvider>
    </BrowserRouter>
  )
}

describe('KnowledgeBase (MemoryAndTools)', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    api.queryAgentMemory.mockResolvedValue({ success: true, data: mockMemoryStats })
    api.getTools.mockResolvedValue({ success: true, tools: mockTools })
    api.inspectMemoryTable.mockResolvedValue(mockTableInspectData)

    api.getStreamConfig.mockResolvedValue({ success: true, is_streaming: false, live_history_window: '1hour' })
    api.getFeatureTypes.mockResolvedValue({ success: true, feature_types: [] })
    api.getAgentStatus.mockResolvedValue({ success: true, agents: {} })
  })


  it('renders the page heading', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('Memory & Tools')).toBeInTheDocument()
    })
  })


  it('renders Memory and Tools tab buttons', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('Memory')).toBeInTheDocument()
    })

    expect(screen.getByText('Tools')).toBeInTheDocument()
  })


  it('shows memory stats cards with correct values', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    expect(screen.getByText('4')).toBeInTheDocument()

    expect(screen.getByText('385')).toBeInTheDocument()
  })


  it('shows table names from memory stats', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    expect(screen.getByText('activity_log')).toBeInTheDocument()
    expect(screen.getByText('user_notes')).toBeInTheDocument()
    expect(screen.getByText('health_insights')).toBeInTheDocument()
  })


  it('shows System badge for reports and activity_log, Agent for others', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    const systemBadges = screen.getAllByText('System')
    const agentBadges = screen.getAllByText('Agent')

    expect(systemBadges.length).toBe(2)
    expect(agentBadges.length).toBe(2)
  })


  it('expands a table when Inspect button is clicked', async () => {
    const user = userEvent.setup()
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    const inspectButtons = screen.getAllByText('Inspect')
    await user.click(inspectButtons[0])

    await waitFor(() => {
      expect(api.inspectMemoryTable).toHaveBeenCalled()
    })

    await waitFor(() => {
      expect(screen.getByText('Test Report')).toBeInTheDocument()
    })

    expect(screen.getByText('Another Report')).toBeInTheDocument()
  })


  it('collapses a table when Collapse button is clicked', async () => {
    const user = userEvent.setup()
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    const inspectButtons = screen.getAllByText('Inspect')
    await user.click(inspectButtons[0])

    await waitFor(() => {
      expect(screen.getByText('Test Report')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Collapse'))

    await waitFor(() => {
      expect(screen.queryByText('Test Report')).not.toBeInTheDocument()
    })
  })


  it('switches to Tools tab and shows tool list', async () => {
    const user = userEvent.setup()
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Tools'))

    await waitFor(() => {
      expect(screen.getByText('sql')).toBeInTheDocument()
    })

    expect(screen.getByText('code')).toBeInTheDocument()
    expect(screen.getByText('push_report')).toBeInTheDocument()
  })


  it('shows tool descriptions on the Tools tab', async () => {
    const user = userEvent.setup()
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Tools'))

    await waitFor(() => {
      expect(screen.getByText(/SQL queries on health_data/i)).toBeInTheDocument()
    })

    expect(screen.getByText(/Execute Python code/i)).toBeInTheDocument()
  })


  it('shows tool parameters on the Tools tab', async () => {
    const user = userEvent.setup()
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Tools'))

    await waitFor(() => {
      expect(screen.getByText('query*')).toBeInTheDocument()
    })

    expect(screen.getByText('code*')).toBeInTheDocument()
  })


  it('shows error message when API fails', async () => {
    api.getTools.mockRejectedValue(new Error('Server unavailable'))
    api.queryAgentMemory.mockRejectedValue(new Error('Server unavailable'))

    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('Server unavailable')).toBeInTheDocument()
    })
  })


  it('shows error when tools API returns success: false', async () => {
    api.getTools.mockResolvedValue({ success: false, error: 'Agent not initialized' })
    api.queryAgentMemory.mockResolvedValue({ success: true, data: mockMemoryStats })

    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('Agent not initialized')).toBeInTheDocument()
    })
  })


  it('shows error when table inspect fails', async () => {
    const user = userEvent.setup()
    api.inspectMemoryTable.mockResolvedValue({
      success: false,
      error: 'Table not found',
    })

    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    const inspectButtons = screen.getAllByText('Inspect')
    await user.click(inspectButtons[0])

    await waitFor(() => {
      expect(screen.getByText('Table not found')).toBeInTheDocument()
    })
  })


  it('shows loading state while fetching data', async () => {
    api.getTools.mockReturnValue(new Promise(() => {}))
    api.queryAgentMemory.mockReturnValue(new Promise(() => {}))

    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('Synchronizing...')).toBeInTheDocument()
    })
  })


  it('displays row counts for each table', async () => {
    renderKnowledgeBase()

    await waitFor(() => {
      expect(screen.getByText('reports')).toBeInTheDocument()
    })

    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('340')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('28')).toBeInTheDocument()
  })
})
