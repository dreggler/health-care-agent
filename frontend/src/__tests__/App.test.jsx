import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, describe, it, expect, beforeEach } from 'vitest'

vi.mock('../lib/api', () => import('../test/mocks/api'))

vi.mock('../pages/Dashboard', () => ({
  default: () => <div data-testid="page-dashboard">Dashboard Page</div>,
}))
vi.mock('../pages/AutonomousAgentMonitor', () => ({
  default: () => <div data-testid="page-agent">Agent Monitor Page</div>,
}))
vi.mock('../pages/ReportsView', () => ({
  default: () => <div data-testid="page-reports">Reports Page</div>,
}))
vi.mock('../pages/PromptEditor', () => ({
  default: () => <div data-testid="page-prompts">Prompts Page</div>,
}))
vi.mock('../pages/KnowledgeBase', () => ({
  default: () => <div data-testid="page-knowledge">Knowledge Page</div>,
}))
vi.mock('../pages/PersonalisedPages', () => ({
  default: () => <div data-testid="page-apps">Personalised Pages Page</div>,
}))

import App from '../App'
import { api } from '../test/mocks/api'

describe('App', () => {
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
    api.getAgentStatus.mockResolvedValue({ success: true, agents: {} })
  })


  it('renders without crashing', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })
  })


  it('renders all navigation links', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })

    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Agent Monitor')).toBeInTheDocument()
    expect(screen.getByText('Reports')).toBeInTheDocument()
    expect(screen.getByText('Prompts')).toBeInTheDocument()
    expect(screen.getByText('Memory & Tools')).toBeInTheDocument()
    expect(screen.getByText('Personalised Pages')).toBeInTheDocument()
  })


  it('renders the HiMe brand and subtitle', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })

    expect(screen.getByText('Digital Avatar')).toBeInTheDocument()
  })


  it('shows Live HealthKit badge (no user selector needed)', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })

    const badges = screen.getAllByText('Live HealthKit')
    expect(badges.length).toBeGreaterThanOrEqual(1)
  })


  it('shows Dashboard page on root route', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByTestId('page-dashboard')).toBeInTheDocument()
    })
  })


  it('navigates to Agent Monitor page when link is clicked', async () => {
    const user = userEvent.setup()
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Agent Monitor')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Agent Monitor'))

    await waitFor(() => {
      expect(window.location.pathname).toBe('/agent')
    })

    expect(screen.getByTestId('page-agent')).toBeInTheDocument()
    expect(screen.getByTestId('page-dashboard')).toBeInTheDocument()
  })


  it('shows green dot next to Agent Monitor when agent is running', async () => {
    api.getAgentStatus.mockResolvedValue({
      success: true,
      agents: {
        LiveUser: { status: 'running', cycles_completed: 5 },
      },
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })

    await waitFor(() => {
      const agentLink = screen.getByText('Agent Monitor').closest('a')
      const dot = agentLink.querySelector('.bg-ok')
      expect(dot).toBeInTheDocument()
    })
  })


  it('does not show green dot when agent is not running', async () => {
    api.getAgentStatus.mockResolvedValue({ success: true, agents: {} })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('HiMe')).toBeInTheDocument()
    })

    await waitFor(() => {
      const agentLink = screen.getByText('Agent Monitor').closest('a')
      const dot = agentLink.querySelector('.bg-ok')
      expect(dot).toBeNull()
    })
  })


  it('shows Agent badge in footer when agent is running', async () => {
    api.getAgentStatus.mockResolvedValue({
      success: true,
      agents: {
        LiveUser: { status: 'running', cycles_completed: 5 },
      },
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Agent')).toBeInTheDocument()
    })
  })


  it('mounts all six persistent pages simultaneously', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByTestId('page-dashboard')).toBeInTheDocument()
    })

    expect(screen.getByTestId('page-agent')).toBeInTheDocument()
    expect(screen.getByTestId('page-reports')).toBeInTheDocument()
    expect(screen.getByTestId('page-prompts')).toBeInTheDocument()
    expect(screen.getByTestId('page-knowledge')).toBeInTheDocument()
    expect(screen.getByTestId('page-apps')).toBeInTheDocument()
  })
})
