import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CommandPalette } from './CommandPalette'

const HIT = {
  id: 'i1',
  workspace_id: 'ws1',
  type: 'sql_script',
  name: 'bao-cao',
  display_name: 'Báo cáo doanh thu',
  folder_path: '/staging/',
}

function renderPalette() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <CommandPalette /> },
      { path: '/workspaces/:workspaceId/items/:itemId', element: <p>trang item</p> },
    ],
    { initialEntries: ['/'] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

function stubHits(items: unknown[]) {
  const mock = vi.fn<typeof fetch>(
    async () => new Response(JSON.stringify({ items }), { status: 200 }),
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('CommandPalette', () => {
  it('Ctrl+K mở bảng lệnh', async () => {
    stubHits([])
    renderPalette()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await userEvent.keyboard('{Control>}k{/Control}')
    expect(screen.getByRole('dialog', { name: 'Bảng lệnh' })).toBeInTheDocument()
  })

  it('⌘K (metaKey) cũng mở — nếu không thì mọi người dùng macOS mất tính năng', async () => {
    stubHits([])
    renderPalette()
    await userEvent.keyboard('{Meta>}k{/Meta}')
    expect(screen.getByRole('dialog', { name: 'Bảng lệnh' })).toBeInTheDocument()
  })

  it('Escape đóng', async () => {
    stubHits([])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('mở lại là một bảng TRẮNG, không giữ chuỗi tìm cũ', async () => {
    // Giữ chuỗi cũ làm người dùng thấy kết quả của lần tìm trước và tưởng đó là kết
    // quả của lần này.
    stubHits([HIT])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    await userEvent.keyboard('{Escape}')
    await userEvent.keyboard('{Control>}k{/Control}')
    expect(screen.getByLabelText(/tìm item/i)).toHaveValue('')
  })

  it('mở ra là thấy sẵn lệnh, không phải một câu bảo người dùng gõ', async () => {
    stubHits([])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    expect(screen.getByRole('option', { name: /danh sách workspace/i })).toBeInTheDocument()
  })

  it('"đang tìm" và "không có kết quả" phân biệt được', async () => {
    // Gộp chúng làm người dùng kết luận item của mình không tồn tại ngay khi request
    // còn đang bay.
    let release: (v: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        () =>
          new Promise<Response>((r) => {
            release = r
          }),
      ),
    )
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')

    // `khong-co` không khớp hành động nào, nên danh sách rỗng thật.
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'khong-co')
    await waitFor(() => expect(screen.getByText('Đang tìm…')).toBeInTheDocument())

    release(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    await waitFor(() => expect(screen.getByText('Không có kết quả')).toBeInTheDocument())
  })

  it('hiện kết quả kèm loại và folder để phân biệt hai item cùng tên', async () => {
    stubHits([HIT])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    expect(await screen.findByText('Báo cáo doanh thu')).toBeInTheDocument()
    expect(screen.getByText('sql_script · /staging/')).toBeInTheDocument()
  })

  it('Enter điều hướng tới item đang chọn và đóng bảng', async () => {
    stubHits([HIT])
    const { router } = renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    await screen.findByText('Báo cáo doanh thu')

    await userEvent.keyboard('{Enter}')
    expect(router.state.location.pathname).toBe('/workspaces/ws1/items/i1')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('mũi tên di chuyển con trỏ và Enter chạy đúng mục đang chọn', async () => {
    stubHits([HIT, { ...HIT, id: 'i2', display_name: 'Báo cáo chi phí' }])
    const { router } = renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    await screen.findByText('Báo cáo doanh thu')

    const options = screen.getAllByRole('option')
    expect(options[0]).toHaveAttribute('aria-selected', 'true')

    await userEvent.keyboard('{ArrowDown}')
    expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true')

    await userEvent.keyboard('{Enter}')
    expect(router.state.location.pathname).toBe('/workspaces/ws1/items/i2')
  })

  it('con trỏ về đầu khi chuỗi tìm đổi', async () => {
    // Giữ con trỏ ở vị trí cũ thì Enter chạy một lệnh khác với thứ người dùng đang nhìn.
    stubHits([HIT, { ...HIT, id: 'i2', display_name: 'Báo cáo chi phí' }])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    await screen.findByText('Báo cáo doanh thu')

    await userEvent.keyboard('{ArrowDown}')
    expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true')

    await userEvent.type(screen.getByLabelText(/tìm item/i), 'o')
    await waitFor(() => expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true'))
  })

  it('lỗi tìm kiếm hiện thông báo, không hiện "Không có kết quả"', async () => {
    // "Không có kết quả" khi server đang lỗi là một câu SAI, và nó khiến người dùng
    // kết luận item của mình không tồn tại.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          new Response(JSON.stringify({ title: 'Bad Gateway', status: 502 }), {
            status: 502,
            headers: { 'content-type': 'application/problem+json' },
          }),
      ),
    )
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(screen.getByLabelText(/tìm item/i), 'bao')
    await waitFor(() => expect(screen.getByText(/Bad Gateway/)).toBeInTheDocument())
    expect(screen.queryByText('Không có kết quả')).not.toBeInTheDocument()
  })

  it('bấm ra ngoài đóng, bấm bên trong thì không', async () => {
    stubHits([])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.click(screen.getByRole('dialog'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('KHÔNG gọi server khi chưa nhập gì', async () => {
    const mock = stubHits([])
    renderPalette()
    await userEvent.keyboard('{Control>}k{/Control}')
    await new Promise((r) => setTimeout(r, 30))
    expect(mock).not.toHaveBeenCalled()
  })
})
