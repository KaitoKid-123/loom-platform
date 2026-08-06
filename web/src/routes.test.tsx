import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

/** Mặc định là một phản hồi `/me` HỢP LỆ kèm `items: []` cho các endpoint danh sách:
 *  `AppLayout` render `AppShell` với người dùng này ở mọi route, nên một `/me` sai hình
 *  dạng làm mọi test trong file đỏ vì một lý do không liên quan đến route. */
function stubFetch(
  body: unknown = {
    subject: 's',
    email: 'e@loom.local',
    display_name: 'Kilgore Trout',
    groups: [],
    items: [],
  },
) {
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
    expect(await screen.findByText(/page not found/i)).toBeInTheDocument()
  })

  it('trang không tìm thấy nói tới khả năng mất quyền', async () => {
    // Backend trả 404 cho tài nguyên người gọi không được đọc (spec mục 4.5), nên
    // nói riêng "trang không tồn tại" sẽ khiến người dùng tưởng dữ liệu bị xoá.
    stubFetch()
    renderAt('/duong/khong/ton/tai')
    expect(await screen.findByText(/no longer have permission/i)).toBeInTheDocument()
  })

  it('bộ lọc Explorer nằm trong query string để deep-link được', async () => {
    stubFetch()
    const { container } = renderAt(
      '/workspaces/11111111-1111-1111-1111-111111111111/items?folder=/staging/&type=pipeline',
    )
    // Không khẳng định nội dung ở đây — chỉ khẳng định route KHỚP và không nổ.
    expect(container.textContent).not.toContain('Page not found')
  })

  it('route connections của một workspace khớp', async () => {
    stubFetch()
    const { container } = renderAt(
      '/workspaces/11111111-1111-1111-1111-111111111111/connections',
    )
    expect(container.textContent).not.toContain('Page not found')
  })
})

describe('⌘K nối dây vào ứng dụng', () => {
  it('mở được từ màn hình gốc, không chỉ khi test riêng component', async () => {
    // Test riêng `CommandPalette` xanh mà component không được mount ở đâu cả thì tính
    // năng không tồn tại với người dùng. Đây là phép kiểm rằng nó CÓ trong cây.
    stubFetch({ subject: 's', email: 'e', display_name: 'Long', groups: [] })
    renderAt('/')
    await screen.findByRole('heading', { name: /workspace/i })

    await userEvent.keyboard('{Control>}k{/Control}')
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
  })
})

describe('route chi tiết item', () => {
  it('bấm một item KHÔNG ra trang không-tìm-thấy', async () => {
    // Explorer và ⌘K đều liên kết tới đường này. Thiếu route thì cả hai hành trình vỡ,
    // dù test riêng của chúng xanh.
    stubFetch({
      id: 'i1',
      workspace_id: 'ws1',
      type: 'sql_script',
      name: 'x',
      display_name: 'X',
      folder_path: '/',
      description: null,
      definition: {},
      version: 1,
      updated_at: '2026-08-05T00:00:00Z',
    })
    const { container } = renderAt('/workspaces/ws1/items/i1')
    // `stubFetch` trả cùng một payload cho mọi URL, kể cả `/versions` — nên test này
    // cũng đi qua đúng đường mà một phản hồi sai hình dạng đi qua.
    await screen.findByRole('heading', { name: 'X' })
    expect(container.textContent).not.toContain('Page not found')
  })
})
