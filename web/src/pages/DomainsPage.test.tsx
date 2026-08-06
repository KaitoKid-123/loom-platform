import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DomainsPage } from './DomainsPage'

const DOMAIN = {
  id: 'd1',
  name: 'tai-chinh',
  display_name: 'Finance',
  description: null,
  workspace_count: 3,
  my_role: null,
}

function stub(tenantRole: string | null, domains: unknown[] = [DOMAIN]) {
  const mock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input)
    if (url.includes('/domains')) {
      return new Response(JSON.stringify({ items: domains, next_cursor: null }), { status: 200 })
    }
    return new Response(
      JSON.stringify({ items: [], next_cursor: null, tenant_role: tenantRole }),
      { status: 200 },
    )
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function renderPage(search = '') {
  const router = createMemoryRouter([{ path: '/domains', element: <DomainsPage /> }], {
    initialEntries: [`/domains${search}`],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DomainsPage', () => {
  it('hiện domain kèm số workspace bên trong', async () => {
    stub('admin')
    renderPage()
    expect(await screen.findByText('Finance')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('người KHÔNG phải admin cấp tenant vẫn ĐỌC được danh sách', async () => {
    // Cố ý khác workspace: danh sách domain là bản đồ tổ chức, và biết phòng Tài chính
    // tồn tại không phải là đọc được dữ liệu của họ.
    stub(null)
    renderPage()
    expect(await screen.findByText('Finance')).toBeInTheDocument()
  })

  it('nhưng KHÔNG thấy nút tạo', async () => {
    stub(null)
    renderPage()
    await screen.findByText('Finance')
    expect(screen.queryByRole('button', { name: /new domain/i })).not.toBeInTheDocument()
  })

  it('admin cấp tenant thấy nút tạo', async () => {
    stub('admin')
    renderPage()
    expect(await screen.findByRole('button', { name: /new domain/i })).toBeInTheDocument()
  })

  it('"không có vai trò" hiện dấu gạch, không phải ô trống', async () => {
    // Ô trống trông như dữ liệu chưa tải xong; dấu gạch là một câu trả lời.
    stub('admin')
    renderPage()
    await screen.findByText('Finance')
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('trạng thái rỗng giải thích domain DÙNG để làm gì', async () => {
    stub('admin', [])
    renderPage()
    expect(await screen.findByText(/no domains yet/i)).toBeInTheDocument()
    expect(screen.getByText(/every workspace inside/i)).toBeInTheDocument()
  })

  it('hộp thoại tạo mở theo URL', async () => {
    stub('admin')
    renderPage('?new=1')
    expect(await screen.findByRole('dialog', { name: 'New domain' })).toBeInTheDocument()
  })

  it('gửi đúng payload khi tạo', async () => {
    const mock = stub('admin')
    renderPage('?new=1')
    await screen.findByRole('dialog', { name: 'New domain' })
    await userEvent.type(screen.getByLabelText(/^name$/i), 'ke-toan')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    const post = mock.mock.calls.find((c) => c[1]?.method === 'POST')
    expect(post).toBeDefined()
    const body = JSON.parse(String(post?.[1]?.body))
    // `display_name` mặc định lấy theo `name` — người dùng bỏ trống không được thành
    // một domain không tên.
    expect(body).toEqual({ name: 'ke-toan', display_name: 'ke-toan' })
  })
})
