import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ItemDetail } from '../../lib/useItem'
import { SqlEditorPanel } from './SqlEditorPanel'

// Giả `SqlEditor` (Monaco) bằng một ô nhập THẬT + hiện `markers`/`completions` ra DOM
// dưới dạng văn bản kiểm được — cùng lý do `ItemPage.test.tsx` giả nó: các bài kiểm ở
// đây canh WIRING của `SqlEditorPanel` (nộp/poll/huỷ/lưu/gợi ý), không canh bản thân
// Monaco (đã có `SqlEditor.test.tsx` riêng).
vi.mock('./SqlEditor', () => ({
  SqlEditor: ({
    value,
    onChange,
    markers,
    completions,
  }: {
    value: string
    onChange?: (v: string) => void
    markers?: Array<{ line: number; column: number; message: string }>
    completions?: Array<{ label: string }>
  }) => (
    <div>
      <textarea
        aria-label="sql"
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
      />
      <pre data-testid="markers">{JSON.stringify(markers ?? [])}</pre>
      <pre data-testid="completions">{JSON.stringify((completions ?? []).map((c) => c.label))}</pre>
    </div>
  ),
}))

const WS = 'ws-1'
const LH = 'lh-1'

const ITEM: ItemDetail = {
  id: 'item-1',
  workspace_id: WS,
  type: 'sql_script',
  name: 'truy-van',
  display_name: 'Truy vấn',
  folder_path: '/',
  description: null,
  definition: { schema_version: 1, sql: 'select 1' },
  version: 1,
  updated_at: '2026-08-05T00:00:00Z',
}

const LAKEHOUSES_PAGE = { items: [{ id: LH, display_name: 'Sales', type: 'lakehouse', name: 'sales', folder_path: '/', version: 1 }], next_cursor: null }

interface Handlers {
  schema?: () => unknown
  onPost?: () => void
  onDelete?: () => void
  statuses?: unknown[]
}

function buildFetchMock({ schema, onPost, onDelete, statuses = [] }: Handlers) {
  let nextQueryId = 0
  const queue = [...statuses]
  const calls: Array<{ method: string; url: string }> = []

  const mock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url })

    if (url.includes('/items?')) {
      return new Response(JSON.stringify(LAKEHOUSES_PAGE), { status: 200 })
    }
    if (url.includes('/schema?')) {
      return new Response(JSON.stringify(schema ? schema() : { namespaces: [] }), { status: 200 })
    }
    if (method === 'POST' && url === '/api/v1/query') {
      onPost?.()
      const queryId = `q${++nextQueryId}`
      return new Response(JSON.stringify({ query_id: queryId }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      })
    }
    if (method === 'DELETE' && url.startsWith('/api/v1/query/')) {
      onDelete?.()
      return new Response(null, { status: 202 })
    }
    if (url.startsWith('/api/v1/query/')) {
      const status = queue.length > 1 ? queue.shift() : (queue[0] ?? { status: 'running' })
      return new Response(JSON.stringify(status), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    throw new Error(`unhandled fetch: ${method} ${url}`)
  })
  return { mock, calls }
}

function renderPanel(fetchMock: typeof fetch) {
  vi.stubGlobal('fetch', fetchMock)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/workspaces/ws-1/items/item-1']}>
        <SqlEditorPanel item={ITEM} etag={'W/"1"'} workspaceId={WS} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function selectLakehouse() {
  await screen.findByRole('option', { name: 'Sales' })
  await userEvent.selectOptions(screen.getByLabelText('Run against'), LH)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SqlEditorPanel — chạy và huỷ (Phần A)', () => {
  it('Cancel gọi ĐÚNG DELETE /api/v1/query/{id} — chứng minh đỏ 1', async () => {
    // Quan sát LỜI GỌI MẠNG, không chỉ trạng thái nút: một `cancel` giả chỉ đổi nhãn nút
    // "Cancel" -> "Cancelled" mà không gọi mạng phải làm bài này ĐỎ.
    const onDelete = vi.fn()
    const { mock } = buildFetchMock({ onDelete })
    renderPanel(mock)
    await selectLakehouse()

    await userEvent.click(screen.getByRole('button', { name: 'Run' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(onDelete).toHaveBeenCalledTimes(1))
    expect(
      mock.mock.calls.some(
        (c) => (c[1]?.method ?? 'GET') === 'DELETE' && String(c[0]).startsWith('/api/v1/query/'),
      ),
    ).toBe(true)
  })

  it('kết quả hiện cờ truncated khi server báo bị cắt — chứng minh đỏ 2', async () => {
    const { mock } = buildFetchMock({
      statuses: [
        {
          status: 'succeeded',
          columns: [{ name: 'id', type: 'int64' }],
          rows: [[1]],
          truncated: true,
          row_count: 50000,
        },
      ],
    })
    renderPanel(mock)
    await selectLakehouse()
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/truncated/i), {
      timeout: 3000,
    })
  })

  it('gạch ĐÚNG dòng/cột lỗi cú pháp server báo (dòng 3) — chứng minh đỏ 3', async () => {
    const mock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/items?')) return new Response(JSON.stringify(LAKEHOUSES_PAGE), { status: 200 })
      if ((init?.method ?? 'GET') === 'POST' && url === '/api/v1/query') {
        return new Response(
          JSON.stringify({
            detail: {
              message: 'the SQL failed to parse',
              errors: [{ line: 3, column: 8, message: "expected FROM, got 'form'" }],
            },
          }),
          { status: 400, headers: { 'content-type': 'application/json' } },
        )
      }
      throw new Error(`unhandled fetch: ${url}`)
    })
    renderPanel(mock)
    await selectLakehouse()
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() =>
      expect(screen.getByTestId('markers')).toHaveTextContent(/"line":3/),
    )
    expect(screen.getByTestId('markers')).toHaveTextContent(/"column":8/)
    // Bảng, không phải trang, hiện lỗi — dùng `role="alert"` cho câu người dùng đọc.
    expect(screen.getByRole('alert')).toHaveTextContent(/failed to parse/i)
  })

  it('403 hiện rõ đây là chuyện QUYỀN, không lẫn với gõ sai', async () => {
    const mock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/items?')) return new Response(JSON.stringify(LAKEHOUSES_PAGE), { status: 200 })
      if ((init?.method ?? 'GET') === 'POST' && url === '/api/v1/query') {
        return new Response(JSON.stringify({ detail: 'you do not have permission to run this query' }), {
          status: 403,
          headers: { 'content-type': 'application/json' },
        })
      }
      throw new Error(`unhandled fetch: ${url}`)
    })
    renderPanel(mock)
    await selectLakehouse()
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/permission/i))
  })

  it('vượt giới hạn (byte cap) hiện rõ "quá lớn", khác thông báo lỗi chung', async () => {
    const { mock } = buildFetchMock({
      statuses: [
        {
          status: 'failed',
          error: 'query would scan 999 bytes, over the 100 byte cap — rejected before reading any data',
        },
      ],
    })
    renderPanel(mock)
    await selectLakehouse()
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() => expect(screen.getByText(/query too large/i)).toBeInTheDocument(), {
      timeout: 3000,
    })
  })
})

describe('SqlEditorPanel — autocomplete tới từ lakehouse đang chọn (Phần C)', () => {
  it('nguồn dữ liệu trả rỗng thì gợi ý rỗng — chứng minh đỏ 6', async () => {
    // KHÔNG phải danh sách cứng: nếu ai đó lỡ trộn một mảng gợi ý mặc định vào, bài
    // này vẫn thấy gợi ý dù server nói lakehouse rỗng — đó là điều phải bị bắt.
    const { mock } = buildFetchMock({ schema: () => ({ namespaces: [] }) })
    renderPanel(mock)
    await selectLakehouse()

    await waitFor(() => expect(screen.getByTestId('completions')).toHaveTextContent('[]'))
  })

  it('gợi ý PHẢN ÁNH ĐÚNG schema của lakehouse đang chọn', async () => {
    const { mock } = buildFetchMock({
      schema: () => ({
        namespaces: [{ name: 'sales', tables: [{ name: 'orders', columns: [{ name: 'id', type: 'int64' }] }] }],
      }),
    })
    renderPanel(mock)
    await selectLakehouse()

    await waitFor(() =>
      expect(screen.getByTestId('completions')).toHaveTextContent('sales.orders'),
    )
    expect(screen.getByTestId('completions')).toHaveTextContent('"id"')
  })

  it('gõ nhiều ký tự liên tiếp KHÔNG gọi lại endpoint schema — chứng minh đỏ 7', async () => {
    const { mock, calls } = buildFetchMock({
      schema: () => ({
        namespaces: [{ name: 'sales', tables: [{ name: 'orders', columns: [] }] }],
      }),
    })
    renderPanel(mock)
    await selectLakehouse()
    await waitFor(() => expect(screen.getByTestId('completions')).toHaveTextContent('sales.orders'))

    const schemaCallsBefore = calls.filter((c) => c.url.includes('/schema?')).length
    expect(schemaCallsBefore).toBe(1)

    const textarea = screen.getByLabelText('sql')
    await userEvent.type(textarea, 'select * from sales.orders')

    const schemaCallsAfter = calls.filter((c) => c.url.includes('/schema?')).length
    expect(schemaCallsAfter).toBe(schemaCallsBefore)
  })
})

describe('SqlEditorPanel — lưu (Phần B), ETag không được nuốt', () => {
  it('412 (ai đó vừa sửa item ở tab khác) hiện rõ, không im lặng', async () => {
    // "Lưu ý ETag" bắt buộc của Phần B: hành vi 412 là của Giai đoạn 1
    // (`ConflictError`/`describeError`) — bài này canh Save button MỚI không phá nó.
    const mock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.includes('/items?')) return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 })
      if ((init?.method ?? 'GET') === 'PATCH') {
        return new Response(
          JSON.stringify({
            title: 'Precondition Failed',
            status: 412,
            detail: 'somebody else changed this item (current version is 5)',
          }),
          { status: 412, headers: { 'content-type': 'application/problem+json' } },
        )
      }
      throw new Error(`unhandled fetch: ${url}`)
    })
    renderPanel(mock)

    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/current version is 5/))
  })
})
