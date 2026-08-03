import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import { navigateTo } from './lib/navigate'

// Chuyển hướng đi qua một module riêng — `window.location` trong jsdom
// là thuộc tính chỉ đọc, giả lập nó rất giòn.
vi.mock('./lib/navigate', () => ({ navigateTo: vi.fn() }))

const USER = { subject: 'CgRsb25n', email: 'long@loom.local', display_name: 'Long' }

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(navigateTo).mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('App', () => {
  it('hiển thị trạng thái đang tải trước khi biết người dùng là ai', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderApp()
    expect(screen.getByRole('status')).toHaveTextContent('Đang tải')
  })

  it('chuyển hướng sang trang đăng nhập khi chưa xác thực', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))
    renderApp()
    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith('/api/v1/auth/login'),
    )
  })

  it('hiển thị shell khi đã đăng nhập', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(USER), { status: 200 })),
    )
    renderApp()
    expect(await screen.findByText('Long')).toBeInTheDocument()
    expect(navigateTo).not.toHaveBeenCalled()
  })

  it('gửi cookie khi gọi /me', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify(USER), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    await screen.findByText('Long')
    const [, init] = fetchMock.mock.calls[0]
    expect(init?.credentials).toBe('include')
  })

  it('hiển thị lỗi khi API hỏng', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 500 })),
    )
    renderApp()
    expect(await screen.findByRole('alert')).toHaveTextContent(/không kết nối được/i)
  })

  it('KHÔNG tự chuyển hướng lại khi vừa đăng nhập hỏng — chặn vòng lặp câm', async () => {
    // `/auth/callback` đưa về `/?error=login_failed`. Nếu App vẫn tự sang
    // /auth/login thì với nguyên nhân dai dẳng nó quay vòng vô tận, không
    // hiện lỗi nào cả.
    vi.stubGlobal('location', {
      ...window.location,
      search: '?error=login_failed',
    })
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))

    renderApp()

    expect(await screen.findByRole('alert')).toHaveTextContent(/không thành công/i)
    expect(screen.getByRole('link', { name: /thử lại/i })).toBeInTheDocument()
    expect(navigateTo).not.toHaveBeenCalled()
  })
})
