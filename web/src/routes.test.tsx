import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { routeObjects } from './routes'

function renderAt(path: string) {
  const router = createMemoryRouter(routeObjects, { initialEntries: [path] })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

function stubFetch(body: unknown = { items: [] }) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('routes', () => {
  it('đường gốc hiện danh sách workspace', async () => {
    stubFetch()
    renderAt('/')
    expect(await screen.findByRole('heading', { name: /workspace/i })).toBeInTheDocument()
  })

  it('đường lạ hiện trang không tìm thấy, KHÔNG phải màn hình trắng', async () => {
    stubFetch()
    renderAt('/duong/khong/ton/tai')
    expect(await screen.findByText(/không tìm thấy trang/i)).toBeInTheDocument()
  })

  it('trang không tìm thấy nói tới khả năng mất quyền', async () => {
    // Backend trả 404 cho tài nguyên người gọi không được đọc (spec mục 4.5), nên
    // nói riêng "trang không tồn tại" sẽ khiến người dùng tưởng dữ liệu bị xoá.
    stubFetch()
    renderAt('/duong/khong/ton/tai')
    expect(await screen.findByText(/không còn quyền/i)).toBeInTheDocument()
  })

  it('bộ lọc Explorer nằm trong query string để deep-link được', async () => {
    stubFetch()
    const { container } = renderAt(
      '/workspaces/11111111-1111-1111-1111-111111111111/items?folder=/staging/&type=pipeline',
    )
    // Không khẳng định nội dung ở đây — chỉ khẳng định route KHỚP và không nổ.
    expect(container.textContent).not.toContain('không tìm thấy trang')
  })

  it('route connections của một workspace khớp', async () => {
    stubFetch()
    const { container } = renderAt(
      '/workspaces/11111111-1111-1111-1111-111111111111/connections',
    )
    expect(container.textContent).not.toContain('không tìm thấy trang')
  })
})
