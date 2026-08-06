import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { atLeast } from '../lib/useWorkspaces'
import { WorkspaceListPage } from './WorkspaceListPage'

const WS = {
  id: '11111111-1111-1111-1111-111111111111',
  name: 'retail',
  display_name: 'Retail',
  description: null,
  domain_id: null,
  my_role: 'contributor',
}

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <WorkspaceListPage /> },
      // Đích của nút "Tạo item". Không có route này thì cú điều hướng ném lỗi và
      // test không phân biệt được "nút không làm gì" với "nút đi sai chỗ".
      { path: '/workspaces/:workspaceId/items', element: <p>trang explorer</p> },
    ],
    { initialEntries: ['/'] },
  )
  // `retryDelay: 0`, KHÔNG `retry: false`: hook tự đặt `retry` của nó nên `retry:
  // false` ở đây bị ghi đè — và đó đúng là điều `useWorkspaces` muốn (không ai cấu
  // hình sai được nó từ bên ngoài). Bỏ độ trễ thay vì bỏ số lần thử: test vẫn đi qua
  // đúng đường retry thật, chỉ không phải chờ backoff 1s + 2s.
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

function stubJson(body: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async () => new Response(JSON.stringify(body), { status })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WorkspaceListPage', () => {
  it('hiện skeleton chứ không phải spinner toàn trang khi đang tải', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise(() => {})),
    )
    renderPage()
    // Quy tắc bắt buộc của spec mục 7.4: KHÔNG spinner toàn trang.
    expect(screen.getByTestId('workspace-skeleton')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('trạng thái rỗng nói rõ bước tiếp theo', async () => {
    stubJson({ items: [], next_cursor: null })
    renderPage()
    // Không chỉ "Không có gì" — phải nói người dùng làm GÌ tiếp.
    expect(await screen.findByText(/ask an administrator/i)).toBeInTheDocument()
    // Và nhắc tới nhóm: vai trò gán cho nhóm cấp quyền y như gán cho cá nhân, và
    // người dùng không tự biết điều đó.
    expect(screen.getByText(/group/i)).toBeInTheDocument()
  })

  it('ẩn nút tạo item khi vai trò là viewer', async () => {
    stubJson({ items: [{ ...WS, my_role: 'viewer' }], next_cursor: null })
    renderPage()
    await screen.findByText('Retail')
    expect(screen.queryByRole('button', { name: /new item/i })).not.toBeInTheDocument()
  })

  it('hiện nút tạo item khi vai trò là contributor', async () => {
    stubJson({ items: [WS], next_cursor: null })
    renderPage()
    expect(await screen.findByRole('button', { name: /new item/i })).toBeInTheDocument()
  })

  it('nút tạo item mở hộp thoại QUA URL, không qua state React', async () => {
    stubJson({ items: [WS], next_cursor: null })
    const { router } = renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /new item/i }))
    // `?new=1` trong URL là thứ làm hộp thoại deep-link và F5 được (spec mục 7.4).
    expect(router.state.location.pathname).toBe(`/workspaces/${WS.id}/items`)
    expect(router.state.location.search).toBe('?new=1')
  })

  it('lỗi tải hiện thông báo của server, không phải "có lỗi"', async () => {
    stubJson({ title: 'Bad Gateway', status: 502 }, 502)
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent(/502/)
  })

  it('vai trò hiện ra để người dùng biết mình là gì trong workspace đó', async () => {
    stubJson({ items: [WS], next_cursor: null })
    renderPage()
    expect(await screen.findByText('contributor')).toBeInTheDocument()
  })
})

describe('atLeast', () => {
  it('khớp đúng thứ tự vai trò của backend', () => {
    expect(atLeast('viewer', 'contributor')).toBe(false)
    expect(atLeast('contributor', 'contributor')).toBe(true)
    expect(atLeast('member', 'contributor')).toBe(true)
    expect(atLeast('admin', 'contributor')).toBe(true)
    expect(atLeast('viewer', 'viewer')).toBe(true)
    expect(atLeast('member', 'admin')).toBe(false)
  })

  it('vai trò KHÔNG nhận ra trả false, không phải true', () => {
    // Mặc định an toàn: nếu backend thêm một vai trò mà frontend chưa biết, ẩn nút là
    // sai-nhưng-vô-hại, còn hiện nút là người dùng bấm rồi ăn 403.
    expect(atLeast('sieu-admin', 'contributor')).toBe(false)
    expect(atLeast('', 'viewer')).toBe(false)
    expect(atLeast('ADMIN', 'viewer')).toBe(false)
  })
})

describe('WorkspaceListPage — tạo workspace', () => {
  function stubList(tenantRole: string | null, items: unknown[] = []) {
    const mock = vi.fn<typeof fetch>(async (input) =>
      String(input).includes('/domains')
        ? new Response(JSON.stringify({ items: [] }), { status: 200 })
        : new Response(
            JSON.stringify({ items, next_cursor: null, tenant_role: tenantRole }),
            { status: 200 },
          ),
    )
    vi.stubGlobal('fetch', mock)
    return mock
  }

  it('admin cấp tenant thấy nút New workspace', async () => {
    // Đây là khoảng trống lộ ra từ một ảnh chụp: API có `POST /workspaces` từ Task 21
    // nhưng không có chỗ nào trên giao diện gọi tới nó.
    stubList('admin')
    renderPage()
    expect(await screen.findByRole('button', { name: /new workspace/i })).toBeInTheDocument()
  })

  it('người không phải admin cấp tenant KHÔNG thấy nút đó', async () => {
    // Server trả 404 cho họ, nên hiện nút là mời điền xong một form rồi mới biết mình
    // không có quyền.
    stubList(null, [WS])
    renderPage()
    await screen.findByText('Retail')
    expect(screen.queryByRole('button', { name: /new workspace/i })).not.toBeInTheDocument()
  })

  it('hộp thoại mở theo URL, không theo state React', async () => {
    stubList('admin')
    const { router } = renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /new workspace/i }))
    expect(router.state.location.search).toBe('?new=1')
    expect(screen.getByRole('dialog', { name: 'New workspace' })).toBeInTheDocument()
  })

  it('nói rõ `name` không đổi được về sau', async () => {
    // Nó đi vào `storage_prefix` ở Giai đoạn 2, nên đây không phải một cái nhãn đổi lúc
    // nào cũng được — và người dùng không tự biết điều đó.
    stubList('admin')
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /new workspace/i }))
    expect(screen.getByText(/cannot be changed later/i)).toBeInTheDocument()
  })
})

describe('WorkspaceListPage — đổi tên', () => {
  function stubOne(role: string) {
    const mock = vi.fn<typeof fetch>(async (input) =>
      String(input).includes('/domains')
        ? new Response(JSON.stringify({ items: [] }), { status: 200 })
        : new Response(
            JSON.stringify({
              items: [{ ...WS, my_role: role, version: 4 }],
              next_cursor: null,
              tenant_role: null,
            }),
            { status: 200 },
          ),
    )
    vi.stubGlobal('fetch', mock)
    return mock
  }

  it('member đổi tên được, và ETag dựng từ version đã có trong danh sách', async () => {
    const mock = stubOne('member')
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Rename' }))
    const input = screen.getByLabelText(/^rename /i)
    await userEvent.clear(input)
    await userEvent.type(input, 'Tên mới{Enter}')

    const patch = mock.mock.calls.find((c) => c[1]?.method === 'PATCH')
    expect(patch).toBeDefined()
    // Không cần một GET phụ để lấy ETag — `version` đã nằm trong phản hồi danh sách.
    expect((patch?.[1]?.headers as Record<string, string>)['If-Match']).toBe('W/"4"')
  })

  it('contributor KHÔNG đổi tên được — cùng cổng server dùng', async () => {
    stubOne('contributor')
    renderPage()
    await screen.findByText('Retail')
    expect(screen.queryByRole('button', { name: 'Rename' })).not.toBeInTheDocument()
  })

  it('Escape huỷ mà không gửi gì', async () => {
    const mock = stubOne('admin')
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Rename' }))
    await userEvent.type(screen.getByLabelText(/^rename /i), 'nửa chừng{Escape}')
    expect(mock.mock.calls.some((c) => c[1]?.method === 'PATCH')).toBe(false)
  })
})
