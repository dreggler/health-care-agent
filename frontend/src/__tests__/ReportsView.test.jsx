import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, describe, it, expect, beforeEach } from 'vitest'

vi.mock('../lib/api', () => import('../test/mocks/api'))

vi.mock('react-markdown', () => ({
  default: ({ children }) => <div data-testid="markdown">{children}</div>,
}))
vi.mock('remark-gfm', () => ({
  default: () => {},
}))

import { api, mockReports } from '../test/mocks/api'
import { AppProvider } from '../context/AppContext'
import { BrowserRouter } from 'react-router-dom'
import ReportsView from '../pages/ReportsView'

function renderReports() {
  return render(
    <BrowserRouter>
      <AppProvider>
        <ReportsView />
      </AppProvider>
    </BrowserRouter>
  )
}

describe('ReportsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    api.queryAgentMemory.mockImplementation((queryType) => {
      if (queryType === 'reports') {
        return Promise.resolve({ success: true, data: mockReports })
      }
      return Promise.resolve({ success: true, data: { table_counts: {}, date_range: {} } })
    })

    api.getStreamConfig.mockResolvedValue({ success: true, is_streaming: false, live_history_window: '1hour' })
    api.getFeatureTypes.mockResolvedValue({ success: true, feature_types: [] })
    api.getAgentStatus.mockResolvedValue({ success: true, agents: {} })
  })


  it('loads and displays reports on mount', async () => {
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    expect(screen.getByText('Daily Activity Goal Achieved')).toBeInTheDocument()
    expect(screen.getByText(/Irregular Heart Rhythm Detected/)).toBeInTheDocument()
  })


  it('renders the page heading', async () => {
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Analysis Reports')).toBeInTheDocument()
    })

    expect(screen.getByText(/archive of agent findings/i)).toBeInTheDocument()
  })


  it('shows empty state when there are no reports', async () => {
    api.queryAgentMemory.mockImplementation((queryType) => {
      if (queryType === 'reports') {
        return Promise.resolve({ success: true, data: [] })
      }
      return Promise.resolve({ success: true, data: { table_counts: {}, date_range: {} } })
    })

    renderReports()

    await waitFor(() => {
      expect(screen.getByText('No reports found')).toBeInTheDocument()
    })

    await waitFor(() => {
      expect(screen.getByText(/adjusting your filters/i)).toBeInTheDocument()
    })
  })


  it('shows an error banner (not an empty state) on API error', async () => {
    api.queryAgentMemory.mockImplementation((queryType) => {
      if (queryType === 'reports') {
        return Promise.reject(new Error('Network error'))
      }
      return Promise.resolve({ success: true, data: { table_counts: {}, date_range: {} } })
    })

    renderReports()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Couldn't load reports/)
    })
    expect(screen.queryByText('No reports found')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })


  it('shows the server error and recovers on Retry when API returns success: false', async () => {
    const user = userEvent.setup()
    api.queryAgentMemory.mockImplementation((queryType) => {
      if (queryType === 'reports') {
        return Promise.resolve({ success: false, error: 'Agent not running' })
      }
      return Promise.resolve({ success: true, data: { table_counts: {}, date_range: {} } })
    })

    renderReports()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Agent not running')
    })

    api.queryAgentMemory.mockImplementation(() => Promise.resolve({ success: true, data: [] }))
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
    expect(screen.getByText('No reports found')).toBeInTheDocument()
  })


  it('renders filter buttons for all alert levels', async () => {
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    const filterSection = screen.getByText('Filter:').parentElement
    expect(filterSection).toBeInTheDocument()

    expect(screen.getByRole('button', { name: 'All' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Critical' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Warning' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Info' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Normal' })).toBeInTheDocument()
  })


  it('filters reports by alert level when filter button is clicked', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: 'Critical' }))

    await waitFor(() => {
      expect(screen.getByText(/Irregular Heart Rhythm Detected/)).toBeInTheDocument()
    })

    expect(screen.queryByText('Elevated Resting Heart Rate Detected')).not.toBeInTheDocument()
    expect(screen.queryByText('Daily Activity Goal Achieved')).not.toBeInTheDocument()
  })


  it('filters reports by search query', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    const searchInput = screen.getByPlaceholderText('Search reports...')
    await user.type(searchInput, 'steps')

    await waitFor(() => {
      expect(screen.getByText('Daily Activity Goal Achieved')).toBeInTheDocument()
    })

    expect(screen.queryByText('Elevated Resting Heart Rate Detected')).not.toBeInTheDocument()
  })


  it('renders sort dropdown and toggle button', async () => {
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Sort by:')).toBeInTheDocument()
    })

    const select = screen.getByDisplayValue('Data Time')
    expect(select).toBeInTheDocument()
  })


  it('opens a report modal when a report card is clicked', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Elevated Resting Heart Rate Detected'))

    await waitFor(() => {
      expect(screen.getByText('Close Report')).toBeInTheDocument()
    })
  })


  it('closes modal when Close Report button is clicked', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Elevated Resting Heart Rate Detected'))

    await waitFor(() => {
      expect(screen.getByText('Close Report')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Close Report'))

    await waitFor(() => {
      expect(screen.queryByText('Close Report')).not.toBeInTheDocument()
    })
  })


  it('refresh button reloads reports', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Refresh')).toBeInTheDocument()
    })

    const reportsCalls = api.queryAgentMemory.mock.calls.filter(c => c[0] === 'reports')
    expect(reportsCalls.length).toBeGreaterThanOrEqual(1)

    const countBefore = api.queryAgentMemory.mock.calls.filter(c => c[0] === 'reports').length

    await user.click(screen.getByText('Refresh'))

    await waitFor(() => {
      const countAfter = api.queryAgentMemory.mock.calls.filter(c => c[0] === 'reports').length
      expect(countAfter).toBeGreaterThan(countBefore)
    })
  })


  it('displays alert level badges on report cards', async () => {
    renderReports()

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })

    const warningBadges = screen.getAllByText('warning')
    expect(warningBadges.length).toBeGreaterThanOrEqual(1)

    const normalBadges = screen.getAllByText('normal')
    expect(normalBadges.length).toBeGreaterThanOrEqual(1)

    const criticalBadges = screen.getAllByText('critical')
    expect(criticalBadges.length).toBeGreaterThanOrEqual(1)
  })


  it('clears search when clear button is clicked', async () => {
    const user = userEvent.setup()
    renderReports()

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Search reports...')).toBeInTheDocument()
    })

    const searchInput = screen.getByPlaceholderText('Search reports...')
    await user.type(searchInput, 'steps')

    await waitFor(() => {
      expect(screen.queryByText('Elevated Resting Heart Rate Detected')).not.toBeInTheDocument()
    })

    const clearButtons = searchInput.parentElement.querySelectorAll('button')
    expect(clearButtons.length).toBeGreaterThan(0)
    await user.click(clearButtons[0])

    await waitFor(() => {
      expect(screen.getByText('Elevated Resting Heart Rate Detected')).toBeInTheDocument()
    })
  })
})
