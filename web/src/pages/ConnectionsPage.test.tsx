import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConnectionsPage } from './ConnectionsPage'

const WS = '11111111-1111-1111-1111-111111111111'

const PG = {
  id: 'c1',
  name: 'pg',
  display_name: 'PG',
  folder_path: '/',
  type: 'connection',
  version: 1,
  definition: {
    schema_version: 1,
    kind: 'postgres',
    host: 'db.local',
    port: 5432,
    secret_ref: 'vault://loom/db#password',
  },
}

function renderPage(items: unknown[] = []) {
  const mock = vi.fn<typeof fetch>(
    async () => new Response(JSON.stringify({ items, next_cursor: null }), { status: 200 }),
  )
  vi.stubGlobal('fetch', mock)
  const router = createMemoryRouter(
    [{ path: '/workspaces/:workspaceId/connections', element: <ConnectionsPage /> }],
    { initialEntries: [`/workspaces/${WS}/connections`] },
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

describe('ConnectionsPage', () => {
  it('ô secret_ref KHÔNG phải input mật khẩu', async () => {
    // `type=password` nói với người dùng "nhập mật khẩu vào đây", và họ sẽ làm đúng thế
    // — rồi credential nằm trong definition và đi vào item_version, audit và Git.
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /add connection/i }))
    expect(screen.getByLabelText(/secret reference/i)).toHaveAttribute('type', 'text')
  })

  it('nói rõ Loom không giữ credential', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /add connection/i }))
    expect(screen.getByText(/loom stores no passwords/i)).toBeInTheDocument()
  })

  it('không có nút kiểm tra kết nối ở Giai đoạn 1', async () => {
    // Nút này thuộc Giai đoạn 3 cùng khung connector. Hiện nó ra mà không chạy được là
    // hứa một tính năng không tồn tại.
    renderPage([PG])
    await screen.findByText('PG')
    expect(screen.queryByRole('button', { name: /test connection/i })).not.toBeInTheDocument()
  })

  it('hiện secret_ref nguyên văn — nó là đường dẫn, không phải bí mật', async () => {
    renderPage([PG])
    expect(await screen.findByText('vault://loom/db#password')).toBeInTheDocument()
  })

  it('hiện host, port và loại nguồn để phân biệt hai connection cùng tên', async () => {
    renderPage([PG])
    await screen.findByText('PG')
    expect(screen.getByText('postgres')).toBeInTheDocument()
    expect(screen.getByText('db.local:5432')).toBeInTheDocument()
  })

  it('chỉ xin item type=connection, không tải cả workspace rồi lọc', async () => {
    // Lọc ở client trên một trang 200 item cho kết quả SAI khi workspace lớn hơn thế.
    const { mock } = renderPage([PG])
    await screen.findByText('PG')
    expect(String(mock.mock.calls[0][0])).toContain('type=connection')
  })

  it('lỗi 422 gắn vào đúng ô, kể cả khi loc lồng trong definition', async () => {
    // `loc` là `['body','definition','secret_ref']` — khoá phải là phần CUỐI, nếu không
    // thông báo không tìm được ô nào và người dùng đọc một câu chung.
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /add connection/i }))

    await userEvent.type(screen.getByLabelText(/^name$/i), 'pg')
    await userEvent.type(screen.getByLabelText(/^host$/i), 'db.local')
    await userEvent.type(screen.getByLabelText(/secret reference/i), 'mat-khau-that-cua-toi')

    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({
              title: 'Unprocessable Content',
              status: 422,
              detail: 'dữ liệu gửi lên không hợp lệ',
              errors: [
                {
                  loc: ['body', 'definition', 'secret_ref'],
                  msg: 'phải là vault://path#key hoặc k8s://namespace/name#key',
                  type: 'value_error',
                },
              ],
            }),
            { status: 422, headers: { 'content-type': 'application/problem+json' } },
          ),
      ),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText(/vault:\/\/path#key/)).toBeInTheDocument()
  })

  it('gửi definition đúng hình dạng backend chờ', async () => {
    const { mock } = renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /add connection/i }))
    await userEvent.type(screen.getByLabelText(/^name$/i), 'pg')
    await userEvent.type(screen.getByLabelText(/^host$/i), 'db.local')
    await userEvent.type(screen.getByLabelText(/secret reference/i), 'vault://loom/db#password')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    const post = mock.mock.calls.find((c) => c[1]?.method === 'POST')
    expect(post).toBeDefined()
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.type).toBe('connection')
    // `port` phải là SỐ: backend khai `port: int`, và gửi chuỗi "5432" ra 422.
    expect(body.definition.port).toBe(5432)
    expect(body.definition).toEqual({
      schema_version: 1,
      kind: 'postgres',
      host: 'db.local',
      port: 5432,
      secret_ref: 'vault://loom/db#password',
    })
  })

  it('trạng thái rỗng nói bước tiếp theo', async () => {
    renderPage([])
    expect(await screen.findByText(/no connections in this workspace/i)).toBeInTheDocument()
    expect(screen.getByText(/pipelines and SQL scripts/i)).toBeInTheDocument()
  })

  it('skeleton khi đang tải, không spinner toàn trang', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise(() => {})),
    )
    const router = createMemoryRouter(
      [{ path: '/workspaces/:workspaceId/connections', element: <ConnectionsPage /> }],
      { initialEntries: [`/workspaces/${WS}/connections`] },
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('connections-skeleton')).toBeInTheDocument()
  })
})
