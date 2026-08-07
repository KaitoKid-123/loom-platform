import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LakehouseSchemaTree } from './LakehouseSchemaTree'

const LAKE_ID = 'a0000000-0000-0000-0000-00000000000a'

const TABLES_ONLY = {
  namespaces: [
    {
      name: 'sales',
      tables: [
        { name: 'orders', columns: null },
        { name: 'customers', columns: null },
      ],
    },
  ],
}

const WITH_COLUMNS = {
  namespaces: [
    {
      name: 'sales',
      tables: [
        { name: 'orders', columns: [{ name: 'id', type: 'int64' }, { name: 'total', type: 'float64' }] },
        { name: 'customers', columns: [{ name: 'id', type: 'int64' }] },
      ],
    },
  ],
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function problemResponse(detail: string, status: number) {
  return new Response(JSON.stringify({ title: 'Forbidden', status, detail }), {
    status,
    headers: { 'content-type': 'application/problem+json' },
  })
}

function renderTree() {
  const mock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input)
    if (url.includes('depth=columns')) return jsonResponse(WITH_COLUMNS)
    return jsonResponse(TABLES_ONLY)
  })
  vi.stubGlobal('fetch', mock)
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <LakehouseSchemaTree lakehouseId={LAKE_ID} />
    </QueryClientProvider>,
  )
  return { ...view, mock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('LakehouseSchemaTree', () => {
  it('hiện skeleton theo HÌNH cây, không phải spinner toàn khối, khi đang tải', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise(() => {})),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <LakehouseSchemaTree lakehouseId={LAKE_ID} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('lakehouse-tree-skeleton')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('lỗi tải hiện NGUYÊN VĂN thông báo của server, không phải "có lỗi"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          problemResponse("you do not have permission to read this lakehouse's schema", 403),
      ),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <LakehouseSchemaTree lakehouseId={LAKE_ID} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /you do not have permission to read this lakehouse's schema/,
    )
  })

  it('cây rỗng nói rõ bước tiếp theo, không chỉ nói "rỗng"', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => jsonResponse({ namespaces: [] })))
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <LakehouseSchemaTree lakehouseId={LAKE_ID} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/no tables yet/i)).toBeInTheDocument()
    // Phải nói BƯỚC TIẾP THEO — spec 7.4 — không chỉ "rỗng".
    expect(screen.getByText(/pipeline|sql script/i)).toBeInTheDocument()
  })

  it('mở lakehouse chỉ gọi depth=tables — CHƯA gọi depth=columns', async () => {
    const { mock } = renderTree()
    await screen.findByText('orders')
    expect(mock.mock.calls.some((c) => String(c[0]).includes('depth=columns'))).toBe(false)
    expect(mock.mock.calls.some((c) => String(c[0]).includes('depth=tables'))).toBe(true)
  })

  it('CHỨNG MINH ĐỎ: mở một bảng ra mới gọi depth=columns, không phải từ đầu', async () => {
    const { mock } = renderTree()
    await screen.findByText('orders')
    expect(mock.mock.calls.some((c) => String(c[0]).includes('depth=columns'))).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: /orders/ }))

    await waitFor(() =>
      expect(mock.mock.calls.some((c) => String(c[0]).includes('depth=columns'))).toBe(true),
    )
  })

  it('cột hiện ra sau khi bảng được mở', async () => {
    renderTree()
    await screen.findByText('orders')
    await userEvent.click(screen.getByRole('button', { name: /orders/ }))
    expect(await screen.findByText('id')).toBeInTheDocument()
    expect(screen.getByText('int64')).toBeInTheDocument()
    expect(screen.getByText('total')).toBeInTheDocument()
  })

  it('mở bảng thứ hai dùng lại cột đã tải, không gọi depth=columns lần hai', async () => {
    const { mock } = renderTree()
    await screen.findByText('orders')
    await userEvent.click(screen.getByRole('button', { name: /orders/ }))
    await screen.findByText('id')

    const callsAfterFirst = mock.mock.calls.filter((c) => String(c[0]).includes('depth=columns')).length
    expect(callsAfterFirst).toBe(1)

    await userEvent.click(screen.getByRole('button', { name: /customers/ }))
    await waitFor(() => expect(screen.getAllByText('id').length).toBeGreaterThan(1))

    const callsAfterSecond = mock.mock.calls.filter((c) => String(c[0]).includes('depth=columns')).length
    expect(callsAfterSecond).toBe(1)
  })

  it('phản hồi thiếu mảng namespaces KHÔNG làm nổ panel', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => jsonResponse({})))
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <LakehouseSchemaTree lakehouseId={LAKE_ID} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/no tables yet/i)).toBeInTheDocument()
  })
})
