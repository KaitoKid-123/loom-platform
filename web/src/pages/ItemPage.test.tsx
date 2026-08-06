import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ItemPage } from './ItemPage'

const WS = '11111111-1111-1111-1111-111111111111'
const ID = 'c0ffee00-0000-0000-0000-000000000001'

const ITEM = {
  id: ID,
  workspace_id: WS,
  type: 'sql_script',
  name: 'bao-cao',
  display_name: 'Báo cáo',
  folder_path: '/staging/',
  description: null,
  definition: { schema_version: 1, sql: 'SELECT 1' },
  version: 3,
  updated_at: '2026-08-05T00:00:00Z',
}

const VERSIONS = [
  { version: 3, display_name: 'Báo cáo', folder_path: '/staging/', description: null, change_note: null, created_at: '2026-08-05T00:00:00Z', created_by: 'u1' },
  { version: 2, display_name: 'Báo cáo cũ', folder_path: '/staging/', description: null, change_note: 'đổi tên', created_at: '2026-08-04T00:00:00Z', created_by: 'u1' },
  { version: 1, display_name: 'Báo cáo', folder_path: '/staging/', description: null, change_note: null, created_at: '2026-08-03T00:00:00Z', created_by: 'u1' },
]

function route(input: RequestInfo | URL): Response {
  const url = String(input)
  if (url.includes('/versions')) {
    return new Response(JSON.stringify({ items: VERSIONS, next_cursor: null }), { status: 200 })
  }
  return new Response(JSON.stringify(ITEM), { status: 200, headers: { etag: 'W/"3"' } })
}

function renderPage() {
  const mock = vi.fn<typeof fetch>(async (input) => route(input))
  vi.stubGlobal('fetch', mock)
  const router = createMemoryRouter(
    [
      { path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> },
      { path: '/workspaces/:workspaceId/items', element: <p>cây item</p> },
    ],
    { initialEntries: [`/workspaces/${WS}/items/${ID}`] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, mock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ItemPage', () => {
  it('phản hồi version thiếu mảng items KHÔNG làm nổ cả trang', async () => {
    // `.items.map` trần sẽ ném và React Router thay CẢ TRANG bằng trang lỗi của nó —
    // người dùng mất luôn metadata và definition vì một phần phụ của trang hỏng.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async (input) =>
        String(input).includes('/versions')
          ? new Response(JSON.stringify({}), { status: 200 })
          : new Response(JSON.stringify(ITEM), { status: 200, headers: { etag: 'W/"3"' } }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> }],
      { initialEntries: [`/workspaces/${WS}/items/${ID}`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: 'Báo cáo' })).toBeInTheDocument()
  })

  it('hiện metadata và version hiện tại', async () => {
    renderPage()
    expect(await screen.findByRole('heading', { name: 'Báo cáo' })).toBeInTheDocument()
    // Version hiện ra vì nó CHÍNH LÀ ETag: khi một lần sửa ăn 412, người dùng đọc được
    // ở đây bản hiện tại là mấy.
    // `aria-label` riêng cho nhãn version: "v3" một mình bị screen reader đọc là
    // "vê ba" mà không nói đó là gì.
    expect(screen.getByLabelText('version 3')).toBeInTheDocument()
    // Nhãn NGƯỜI ĐỌC ĐƯỢC, không phải slug: `typeLabel` đổi `sql_script` thành
    // "SQL script". Slug kỹ thuật chỉ nên xuất hiện ở chỗ nó là dữ liệu.
    expect(screen.getByText('SQL script')).toBeInTheDocument()
  })

  it('hiện definition dưới dạng chỉ đọc, không phải ô nhập', async () => {
    // Một ô sửa được mà không lưu được tệ hơn một ô chỉ đọc — trình soạn thảo là Giai đoạn 2.
    renderPage()
    await screen.findByRole('heading', { name: 'Báo cáo' })
    expect(screen.getByText(/SELECT 1/)).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('liệt kê lịch sử version', async () => {
    renderPage()
    expect(await screen.findByText('v2')).toBeInTheDocument()
    expect(screen.getByText('đổi tên')).toBeInTheDocument()
  })

  it('nút phục hồi của version HIỆN TẠI bị vô hiệu', async () => {
    // Bấm nó chỉ sinh một version mới nội dung y hệt, kèm một dòng audit vô nghĩa.
    renderPage()
    await screen.findByText('v2')
    const buttons = screen.getAllByRole('button', { name: 'Restore' })
    expect(buttons[0]).toBeDisabled()
    expect(buttons[1]).toBeEnabled()
  })

  it('phục hồi gọi đúng endpoint restore', async () => {
    const { mock } = renderPage()
    await screen.findByText('v2')
    await userEvent.click(screen.getAllByRole('button', { name: 'Restore' })[1])
    await waitFor(() =>
      expect(
        mock.mock.calls.some((c) => String(c[0]).endsWith('/versions/2/restore')),
      ).toBe(true),
    )
  })

  it('nói rõ phục hồi sinh version MỚI, không ghi đè lịch sử', async () => {
    renderPage()
    await screen.findByText('v2')
    expect(screen.getByText(/nothing is lost/i)).toBeInTheDocument()
  })

  it('404 nói tới khả năng mất quyền, không chỉ "không tồn tại"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () => new Response(JSON.stringify({ title: 'Not Found', status: 404 }), { status: 404 }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> }],
      { initialEntries: [`/workspaces/${WS}/items/${ID}`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/no longer have permission/i)
  })
})
