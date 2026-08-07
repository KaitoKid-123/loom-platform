import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ItemPage } from './ItemPage'

/**
 * File RIÊNG (basename không trùng `ItemPage.test.tsx`/`ItemPageSqlEditorLoading.test.tsx`):
 * hai chứng minh đỏ của Giai đoạn 2c Phần B ("một câu SQL LÀ một item, có version") cần
 * một backend giả CÓ TRẠNG THÁI (version, ETag, lịch sử) phản ứng đúng ngữ nghĩa
 * `canonical_hash` thật của server — khác hẳn khuôn `route()` không trạng thái của
 * `ItemPage.test.tsx`, nên tách riêng thay vì nhét thêm vào đó.
 */
vi.mock('../components/Editor/SqlEditor', () => ({
  SqlEditor: ({
    value,
    onChange,
  }: {
    value: string
    onChange?: (v: string) => void
  }) => <textarea aria-label="sql" value={value} onChange={(e) => onChange?.(e.target.value)} />,
}))

const WS = '11111111-1111-1111-1111-111111111111'
const ID = 'c0ffee00-0000-0000-0000-00000000000a'

interface VersionRow {
  version: number
  definition: Record<string, unknown>
  display_name: string
  folder_path: string
  description: string | null
  change_note: string | null
  created_at: string
  created_by: string
}

/** Bản giả RÚT GỌN của ngữ nghĩa `item_store.py`: so `definition` đã chuẩn hoá (khoá
 * sắp xếp, cùng tinh thần `canonical_hash`) trước khi quyết định có bump version hay
 * không. Nếu `SqlEditorPanel` lỡ trộn thêm một trường tự sinh (vd. `saved_at`) vào
 * definition trước khi PATCH, hàm `canonical` dưới đây sẽ thấy nó KHÁC bản cũ và bump
 * version — đúng chứng minh đỏ 4 yêu cầu ("cho editor bỏ qua canonical_hash... phải ĐỎ").
 */
function canonical(def: Record<string, unknown>): string {
  return JSON.stringify(def, Object.keys(def).sort())
}

function makeServer() {
  let version = 1
  let definition: Record<string, unknown> = { schema_version: 1, sql: 'select 1' }
  const history: VersionRow[] = [
    {
      version: 1,
      definition,
      display_name: 'Truy vấn',
      folder_path: '/',
      description: null,
      change_note: 'created',
      created_at: '2026-08-01T00:00:00Z',
      created_by: 'u1',
    },
  ]

  function etag() {
    return `W/"${version}"`
  }
  function currentItem() {
    return {
      id: ID,
      workspace_id: WS,
      type: 'sql_script',
      name: 'truy-van',
      display_name: 'Truy vấn',
      folder_path: '/',
      description: null,
      definition,
      version,
      updated_at: '2026-08-05T00:00:00Z',
    }
  }

  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const method = init?.method ?? 'GET'

    if (url.includes('/items?')) {
      // Danh sách lakehouse cho ô chọn "Run against" — rỗng, không liên quan hai bài
      // kiểm này.
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 })
    }
    if (/\/versions\/(\d+)\/restore$/.test(url)) {
      const restoreVersion = Number(/\/versions\/(\d+)\/restore$/.exec(url)![1])
      const old = history.find((h) => h.version === restoreVersion)!
      version += 1
      definition = old.definition
      history.push({
        ...old,
        version,
        change_note: `restored from v${restoreVersion}`,
        created_at: '2026-08-06T00:00:00Z',
      })
      return new Response(JSON.stringify(currentItem()), {
        status: 200,
        headers: { etag: etag() },
      })
    }
    if (url.includes('/versions')) {
      return new Response(
        JSON.stringify({
          items: history
            .slice()
            .reverse()
            .map(({ definition: _d, ...row }) => row),
          next_cursor: null,
        }),
        { status: 200 },
      )
    }
    if (method === 'PATCH' && url === `/api/v1/items/${ID}`) {
      const body = JSON.parse(String(init?.body)) as { definition: Record<string, unknown> }
      if (canonical(body.definition) !== canonical(definition)) {
        version += 1
        definition = body.definition
        history.push({
          version,
          definition,
          display_name: 'Truy vấn',
          folder_path: '/',
          description: null,
          change_note: null,
          created_at: '2026-08-05T12:00:00Z',
          created_by: 'u1',
        })
      }
      return new Response(JSON.stringify(currentItem()), {
        status: 200,
        headers: { etag: etag() },
      })
    }
    if (url === `/api/v1/items/${ID}`) {
      return new Response(JSON.stringify(currentItem()), {
        status: 200,
        headers: { etag: etag() },
      })
    }
    throw new Error(`unhandled fetch: ${method} ${url}`)
  })

  return fetchMock
}

function renderSqlPage(fetchMock: typeof fetch) {
  vi.stubGlobal('fetch', fetchMock)
  const router = createMemoryRouter(
    [{ path: '/workspaces/:workspaceId/items/:itemId', element: <ItemPage /> }],
    { initialEntries: [`/workspaces/${WS}/items/${ID}`] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0, retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ItemPage — lưu một sql_script qua đường editor (Giai đoạn 2c Phần B)', () => {
  it('lưu HAI LẦN không sửa gì không sinh version mới — chứng minh đỏ 4', async () => {
    renderSqlPage(makeServer())

    await screen.findByLabelText('version 1')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByText(/saving/i)).not.toBeInTheDocument())
    // Vẫn v1 — nội dung không đổi, `canonical_hash` phải chặn version mới.
    expect(screen.getByLabelText('version 1')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByText(/saving/i)).not.toBeInTheDocument())
    expect(screen.getByLabelText('version 1')).toBeInTheDocument()
  })

  it('sửa nội dung rồi lưu MỚI sinh version mới', async () => {
    renderSqlPage(makeServer())

    await screen.findByLabelText('version 1')
    const textarea = screen.getByLabelText('sql')
    await userEvent.clear(textarea)
    await userEvent.type(textarea, 'select 2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByLabelText('version 2')).toBeInTheDocument())
  })

  it('phục hồi một version cũ rồi mở lại editor phải thấy đúng nội dung cũ — chứng minh đỏ 5', async () => {
    const fetchMock = makeServer()
    // Đưa item lên v2 (`select 2`) TRƯỚC khi trang mở, để có một v1 (`select 1`) trong
    // lịch sử để phục hồi về.
    await fetchMock('/api/v1/items/' + ID, {
      method: 'PATCH',
      body: JSON.stringify({ definition: { schema_version: 1, sql: 'select 2' } }),
    })

    renderSqlPage(fetchMock)
    await screen.findByLabelText('version 2')
    expect(screen.getByLabelText('sql')).toHaveValue('select 2')

    // Lịch sử liệt kê MỚI NHẤT trước (v2, v1) — nút Restore của v2 (đang hiện hành) bị
    // vô hiệu, nên bảng cần đúng NÚT THỨ HAI (v1), cùng khuôn `ItemPage.test.tsx`.
    const restoreButtons = await screen.findAllByRole('button', { name: 'Restore' })
    await userEvent.click(restoreButtons[1]!)

    await waitFor(() => expect(screen.getByLabelText('version 3')).toBeInTheDocument())
    // Version MỚI (v3), nhưng NỘI DUNG của v1 — restore sinh version mới mang nội dung
    // cũ, không ghi đè lịch sử.
    expect(screen.getByLabelText('sql')).toHaveValue('select 1')
  })
})
