import type { ReactElement } from 'react'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ItemPage } from './ItemPage'

/**
 * File RIÊNG (không phải thêm vào `ItemPage.test.tsx`): bài dưới đây cần kiểm soát THỜI
 * ĐIỂM `import('../components/Editor/SqlEditor')` resolve — mock đồng bộ dùng ở
 * `ItemPage.test.tsx` không cho làm vậy, và trộn hai kiểu mock trong cùng file dễ nhầm
 * bài nào dùng mock nào.
 *
 * Chứng minh: chunk Monaco tải TRÌ HOÃN phải đi kèm một trạng thái chờ ra hình — quy tắc
 * bắt buộc spec 7.4 "skeleton theo hình nội dung sắp tới", không phải một ô trắng.
 */
interface FakeSqlEditorModule {
  SqlEditor: () => ReactElement
}

// Biến giữ resolver của promise mock bên dưới — gán THẬT bên trong chính factory của
// `vi.mock`, và đọc lại trong bài kiểm để tự tay quyết định lúc nào "chunk tải xong".
// KHÔNG cần `vi.hoisted`: factory chỉ thật sự CHẠY khi `import()` diễn ra lúc render (sau
// khi toàn bộ code cấp module này đã chạy xong), lúc đó `releaseImport` đã có giá trị.
let releaseImport: (mod: FakeSqlEditorModule) => void = () => {}
vi.mock('../components/Editor/SqlEditor', () => {
  return new Promise<FakeSqlEditorModule>((resolve) => {
    releaseImport = (mod) => resolve(mod)
  })
})

const WS = '11111111-1111-1111-1111-111111111111'
const ID = 'c0ffee00-0000-0000-0000-000000000003'
const SQL_ITEM = {
  id: ID,
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ItemPage — trạng thái chờ Monaco', () => {
  it('hiện skeleton theo HÌNH trình soạn code trong lúc chunk Monaco còn đang tải, không phải ô trắng', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async (input) =>
        String(input).includes('/versions')
          ? new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 })
          : new Response(JSON.stringify(SQL_ITEM), { status: 200, headers: { etag: 'W/"1"' } }),
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

    // Item đã tải xong (metadata hiện ra) nhưng chunk Monaco thì CHƯA — `releaseImport`
    // chưa được gọi, nên `import()` vẫn treo lơ lửng đúng như lúc mạng còn đang tải.
    await screen.findByRole('heading', { name: 'Truy vấn' })
    expect(screen.getByTestId('sql-editor-skeleton')).toBeInTheDocument()
    // Không phải spinner toàn khối — quy tắc bắt buộc spec 7.4.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    // Bây giờ mới "tải xong" chunk — skeleton phải nhường chỗ cho nội dung thật.
    await act(async () => {
      releaseImport({ SqlEditor: () => <div data-testid="sql-editor-resolved" /> })
    })
    expect(await screen.findByTestId('sql-editor-resolved')).toBeInTheDocument()
    expect(screen.queryByTestId('sql-editor-skeleton')).not.toBeInTheDocument()
  })
})
