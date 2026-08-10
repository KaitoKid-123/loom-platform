import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WorkspacePane } from './WorkspacePane'

const WS = '11111111-1111-1111-1111-111111111111'

const WORKSPACE = {
  id: WS,
  name: 'retail',
  display_name: 'Retail analytics',
  description: null,
  domain_id: null,
  my_role: 'contributor',
}

function renderAt(path: string, items: unknown[] = [WORKSPACE]) {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify({ items, next_cursor: null }), { status: 200 }),
    ),
  )
  const router = createMemoryRouter(
    [
      { path: '/', element: <WorkspacePane /> },
      { path: '/workspaces/:workspaceId/items', element: <WorkspacePane /> },
      { path: '/workspaces/:workspaceId/items/:itemId', element: <WorkspacePane /> },
      { path: '/workspaces/:workspaceId/connections', element: <WorkspacePane /> },
    ],
    { initialEntries: [path] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkspacePane', () => {
  it('hiện TÊN workspace đang mở', async () => {
    // Đây là lỗi cũ: header ghi "Chưa chọn workspace" mãi mãi, kể cả khi đang đứng
    // trong một workspace, vì không chỗ nào đọc được `:workspaceId`.
    renderAt(`/workspaces/${WS}/items`)
    expect(await screen.findByText('Retail analytics')).toBeInTheDocument()
  })

  it('đọc được workspaceId nằm ở route CON, không chỉ route của chính panel', async () => {
    // `useParams` trong một layout chỉ trả tham số của chính route layout đó, nên nó
    // KHÔNG thấy `:workspaceId`. `useMatch` là thứ sửa điều đó.
    renderAt(`/workspaces/${WS}/items/some-item-id`)
    expect(await screen.findByText('Retail analytics')).toBeInTheDocument()
  })

  it('hiện vai trò của người gọi trong workspace đó', async () => {
    renderAt(`/workspaces/${WS}/items`)
    expect(await screen.findByText('contributor')).toBeInTheDocument()
  })

  it('KHÔNG render gì khi chưa vào workspace nào', () => {
    // Trả `null` để `AppShell` bỏ hẳn cột: một cột trống rộng 224px là khoảng vô nghĩa
    // chiếm chỗ của nội dung.
    const { container } = renderAt('/')
    expect(container).toBeEmptyDOMElement()
  })

  it('có liên kết tới All items và Connections của cùng workspace', async () => {
    // Không có liên kết nào thì route `/connections` không tới được, và trang đó là code
    // chết dù test riêng của nó xanh.
    renderAt(`/workspaces/${WS}/items`)
    expect(await screen.findByRole('link', { name: 'All items' })).toHaveAttribute(
      'href',
      `/workspaces/${WS}/items`,
    )
    expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute(
      'href',
      `/workspaces/${WS}/connections`,
    )
  })

  it('"All items" KHÔNG sáng khi đang xem Connections', async () => {
    // Không có `end`, mục này sáng ở mọi đường bắt đầu bằng cùng tiền tố, và người dùng
    // thấy hai mục cùng sáng một lúc.
    renderAt(`/workspaces/${WS}/connections`)
    const all = await screen.findByRole('link', { name: 'All items' })
    expect(all).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('phản hồi thiếu mảng items KHÔNG làm nổ cả vỏ ứng dụng', async () => {
    // Panel này nằm trong vỏ, nên một `.find` ném ở đây thay CẢ MÀN HÌNH bằng trang lỗi
    // của React Router — mất luôn header, rail và trang đang xem.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify({}), { status: 200 })),
    )
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items', element: <WorkspacePane /> }],
      { initialEntries: [`/workspaces/${WS}/items`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('link', { name: 'All items' })).toBeInTheDocument()
  })
})

describe('WorkspacePane — Lakehouse Explorer', () => {
  const LAKE_ID = 'b0000000-0000-0000-0000-00000000000b'

  function stubFor(itemType: string) {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async (input) => {
        const url = String(input)
        if (url.includes(`/items/${LAKE_ID}`)) {
          return new Response(
            JSON.stringify({
              id: LAKE_ID,
              workspace_id: WS,
              type: itemType,
              name: 'sales-lake',
              display_name: 'Sales lake',
              folder_path: '/',
              description: null,
              definition: { schema_version: 1 },
              version: 1,
              updated_at: '2026-08-05T00:00:00Z',
            }),
            { status: 200, headers: { etag: 'W/"1"' } },
          )
        }
        if (url.includes('/schema')) {
          return new Response(JSON.stringify({ namespaces: [] }), { status: 200 })
        }
        return new Response(
          JSON.stringify({ items: [WORKSPACE], next_cursor: null }),
          { status: 200 },
        )
      }),
    )
  }

  function renderItemRoute() {
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items/:itemId', element: <WorkspacePane /> }],
      { initialEntries: [`/workspaces/${WS}/items/${LAKE_ID}`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    return render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }

  it('hiện cây Tables khi item đang mở là một lakehouse', async () => {
    stubFor('lakehouse')
    renderItemRoute()
    expect(await screen.findByText('Tables')).toBeInTheDocument()
  })

  it('KHÔNG hiện cây Tables khi item đang mở không phải lakehouse', async () => {
    stubFor('sql_script')
    renderItemRoute()
    // Chờ panel tải xong (tên workspace hiện ra) trước khi khẳng định vắng mặt, để
    // không đọc nhầm "chưa tải xong" thành "không có".
    await screen.findByText('Retail analytics')
    expect(screen.queryByText('Tables')).not.toBeInTheDocument()
  })

  it('vẫn giữ nav All items/Connections khi đang xem một lakehouse', async () => {
    // Cây Explorer THÊM VÀO, không THAY nav — người dùng vẫn cần đường quay lại danh
    // sách item trong lúc đang duyệt bảng.
    stubFor('lakehouse')
    renderItemRoute()
    await screen.findByText('Tables')
    expect(screen.getByRole('link', { name: 'All items' })).toBeInTheDocument()
  })
})

describe('WorkspacePane — không để lại cột trống', () => {
  it('KHÔNG dựng aside khi chưa vào workspace nào', () => {
    // `AppShell` không kiểm được điều này: `sidebar` là một phần tử JSX nên nó luôn
    // truthy, và `{sidebar && …}` vẫn dựng một cột trống rộng 224px ở trang gốc. Đã
    // thấy trên ảnh chụp thật trước khi sửa.
    const { container } = renderAt('/')
    expect(container.querySelector('aside')).toBeNull()
  })

  it('CÓ dựng aside khi đang trong workspace', async () => {
    const { container } = renderAt(`/workspaces/${WS}/items`)
    await screen.findByText('Retail analytics')
    expect(container.querySelector('aside')).toBeInTheDocument()
  })
})
