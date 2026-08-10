import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ItemPage } from './ItemPage'

// `sql_script` mở bằng Monaco thật (Giai đoạn 2c) — giả lập nó ở ĐÂY để các bài kiểm
// chung của trang (metadata, lịch sử version, phục hồi…) không phải tải Monaco thật
// trong jsdom (canvas/ResizeObserver không có ở đó). Wiring lazy-load riêng của Monaco
// có bài kiểm CHUYÊN BIỆT ở describe cuối file, và bản thân component thật có
// `SqlEditor.test.tsx` riêng.
vi.mock('../components/Editor/SqlEditor', () => ({
  SqlEditor: (props: { value: string }) => <div data-testid="mock-sql-editor">{props.value}</div>,
}))

const WS = '11111111-1111-1111-1111-111111111111'
const ID = 'c0ffee00-0000-0000-0000-000000000001'

// Loại `pipeline` chứ không `sql_script` cho khuôn CHUNG: những bài kiểm dưới đây (metadata,
// lịch sử version, phục hồi, 404…) không liên quan gì tới việc `sql_script` mở bằng Monaco
// — dùng một loại khác giữ chúng độc lập với thay đổi đó. Hành vi riêng của `sql_script`
// có describe riêng ở cuối file.
const ITEM = {
  id: ID,
  workspace_id: WS,
  type: 'pipeline',
  name: 'bao-cao',
  display_name: 'Báo cáo',
  folder_path: '/staging/',
  description: null,
  definition: { schema_version: 1, steps: ['extract', 'load'] },
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
    // Nhãn NGƯỜI ĐỌC ĐƯỢC, không phải slug: `typeLabel` đổi `pipeline` thành "Pipeline".
    // Slug kỹ thuật chỉ nên xuất hiện ở chỗ nó là dữ liệu.
    expect(screen.getByText('Pipeline')).toBeInTheDocument()
  })

  it('hiện definition dưới dạng chỉ đọc, không phải ô nhập (loại KHÔNG PHẢI sql_script)', async () => {
    // Một ô sửa được mà không lưu được tệ hơn một ô chỉ đọc — trình soạn thảo thật (Monaco)
    // chỉ tồn tại cho `sql_script` từ Giai đoạn 2c, xem describe riêng cuối file.
    renderPage()
    await screen.findByRole('heading', { name: 'Báo cáo' })
    expect(screen.getByText(/extract/)).toBeInTheDocument()
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

describe('ItemPage — đọc nội dung một version', () => {
  it('mở một version ra xem definition của nó', async () => {
    // Không có nó thì "Restore" là một nút bấm mù: người dùng không đọc được nội dung cũ
    // trước khi quyết định.
    const mock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (/\/versions\/\d+$/.test(url)) {
        return new Response(
          JSON.stringify({ ...VERSIONS[1], definition: { schema_version: 1, sql: 'SELECT cu' } }),
          { status: 200 },
        )
      }
      return route(input)
    })
    vi.stubGlobal('fetch', mock)
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

    await userEvent.click(await screen.findByRole('button', { name: /v2/ }))
    expect(await screen.findByText(/SELECT cu/)).toBeInTheDocument()
  })

  it('KHÔNG tải nội dung version nào cho tới khi người dùng mở', async () => {
    // Tải sẵn cả lịch sử là kéo về mọi `secret_ref` từng có với item `connection`.
    const mock = vi.fn<typeof fetch>(async (input) => route(input))
    vi.stubGlobal('fetch', mock)
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
    await screen.findByText('v2')
    expect(mock.mock.calls.some((c) => /\/versions\/\d+$/.test(String(c[0])))).toBe(false)
  })
})

describe('ItemPage — sql_script mở bằng Monaco (React.lazy)', () => {
  const SQL_ID = 'c0ffee00-0000-0000-0000-000000000002'
  const SQL_ITEM = {
    ...ITEM,
    id: SQL_ID,
    type: 'sql_script',
    display_name: 'Truy vấn doanh thu',
    definition: { schema_version: 1, sql: 'select 1' },
  }

  function renderSqlPage() {
    const mock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.includes('/versions')) {
        return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 })
      }
      return new Response(JSON.stringify(SQL_ITEM), { status: 200, headers: { etag: 'W/"3"' } })
    })
    vi.stubGlobal('fetch', mock)
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> }],
      { initialEntries: [`/workspaces/${WS}/items/${SQL_ID}`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    return render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }

  it('mở Monaco với nội dung sql của definition, không phải JSON thô', async () => {
    renderSqlPage()
    await screen.findByRole('heading', { name: 'Truy vấn doanh thu' })
    expect(await screen.findByTestId('mock-sql-editor')).toHaveTextContent('select 1')
    // Loại khác dùng `<pre>` JSON thô — `sql_script` thì KHÔNG, đây chính là khác biệt
    // mà Giai đoạn 2c thêm vào.
    expect(screen.queryByText(/"schema_version"/)).not.toBeInTheDocument()
  })

  it('KHÔNG ném khi definition thiếu trường sql', async () => {
    // Phòng vệ giống mọi chỗ khác trong trang này: một item hỏng dữ liệu không được phép
    // làm nổ cả trang.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async (input) =>
        String(input).includes('/versions')
          ? new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 })
          : new Response(JSON.stringify({ ...SQL_ITEM, definition: { schema_version: 1 } }), {
              status: 200,
              headers: { etag: 'W/"3"' },
            }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> }],
      { initialEntries: [`/workspaces/${WS}/items/${SQL_ID}`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('mock-sql-editor')).toHaveTextContent('')
  })
})
