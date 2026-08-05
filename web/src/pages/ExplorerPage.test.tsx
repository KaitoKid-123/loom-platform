import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExplorerPage } from './ExplorerPage'

const WS = '11111111-1111-1111-1111-111111111111'

const item = (name: string, folder = '/', type = 'sql_script') => ({
  id: `id-${name}`,
  name,
  display_name: name,
  folder_path: folder,
  type,
  version: 1,
})

function renderPage(search = '') {
  const router = createMemoryRouter(
    [
      { path: '/workspaces/:workspaceId/items', element: <ExplorerPage /> },
      { path: '/workspaces/:workspaceId/items/:itemId', element: <p>trang item</p> },
    ],
    { initialEntries: [`/workspaces/${WS}/items${search}`] },
  )
  // `retryDelay: 0` chứ không `retry: false`: hook tự đặt `retry` nên `retry: false`
  // bị ghi đè, và đó là điều `useItems` muốn. Xem `WorkspaceListPage.test.tsx`.
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

function stubItems(items: unknown[], nextCursor: string | null = null) {
  const mock = vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify({ items, next_cursor: nextCursor }), { status: 200 }),
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ExplorerPage', () => {
  it('hiện skeleton chứ không spinner toàn trang khi đang tải', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise(() => {})),
    )
    renderPage()
    expect(screen.getByTestId('items-skeleton')).toBeInTheDocument()
  })

  it('trạng thái rỗng của workspace nói bước tiếp theo', async () => {
    stubItems([])
    renderPage()
    expect(await screen.findByText(/chưa có item nào/i)).toBeInTheDocument()
    // Gợi ý trỏ tới nút, và nút có thật — hai khẳng định riêng, vì một trạng thái rỗng
    // bảo người dùng bấm một nút không tồn tại còn tệ hơn không gợi ý gì.
    expect(screen.getByText(/để bắt đầu/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Tạo item' })).toBeInTheDocument()
  })

  it('trạng thái rỗng khi LỌC nói khác với khi workspace trống', async () => {
    // Gộp hai câu làm người dùng vừa đặt bộ lọc tưởng workspace của mình trống.
    stubItems([])
    renderPage('?type=pipeline')
    expect(await screen.findByText(/không có item nào thuộc loại pipeline/i)).toBeInTheDocument()
    expect(screen.queryByText(/chưa có item nào/i)).not.toBeInTheDocument()
  })

  it('dựng cây và hiện item', async () => {
    stubItems([item('bao-cao'), item('trong-folder', '/staging/')])
    renderPage()
    expect(await screen.findByText('bao-cao')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /staging/ })).toBeInTheDocument()
  })

  it('folder đóng thì item bên trong KHÔNG hiện', async () => {
    stubItems([item('an-trong-folder', '/staging/')])
    renderPage()
    const folder = await screen.findByRole('button', { name: /staging/ })
    expect(folder).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('an-trong-folder')).not.toBeInTheDocument()

    await userEvent.click(folder)
    expect(folder).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('an-trong-folder')).toBeInTheDocument()
  })

  it('deep-link vào một folder mở sẵn nhánh đó', async () => {
    // Không có nó thì đường dẫn vừa được đồng nghiệp gửi hiện ra một cây đóng kín và
    // người dùng phải tự bấm mở lại đúng nhánh đã nằm trong URL.
    stubItems([item('sau-hai-tang', '/a/b/')])
    renderPage('?folder=/a/b/')
    expect(await screen.findByText('sau-hai-tang')).toBeInTheDocument()
  })

  it('đổi bộ lọc đổi URL, không chỉ đổi giao diện', async () => {
    stubItems([item('x')])
    const { router } = renderPage()
    await screen.findByText('x')

    await userEvent.selectOptions(screen.getByLabelText('Loại'), 'pipeline')
    expect(router.state.location.search).toBe('?type=pipeline')

    await userEvent.selectOptions(screen.getByLabelText('Loại'), '')
    expect(router.state.location.search).toBe('')
  })

  it('bộ lọc trong URL được gửi lên server, không lọc ở client', async () => {
    // Lọc ở client trên một trang 200 item cho kết quả SAI khi workspace lớn hơn thế.
    const mock = stubItems([item('x', '/', 'pipeline')])
    renderPage('?type=pipeline')
    await screen.findByText('x')
    const url = String(mock.mock.calls[0][0])
    expect(url).toContain('type=pipeline')
    expect(url).toContain('limit=200')
  })

  it('đổi bộ lọc KHÔNG nhồi history — Back về chỗ người dùng đến từ', async () => {
    stubItems([item('x')])
    const { router } = renderPage()
    await screen.findByText('x')
    const before = router.state.historyAction

    await userEvent.selectOptions(screen.getByLabelText('Loại'), 'pipeline')
    expect(router.state.historyAction).toBe('REPLACE')
    expect(before).not.toBe('REPLACE')
  })

  it('nói ra khi cây bị cắt, thay vì hiện thiếu trong im lặng', async () => {
    // Đây là phép canh một IM LẶNG: không có nó, workspace 300 item hiện 200 và cây
    // trông y như một cây đầy đủ.
    stubItems([item('x')], 'con-nua')
    renderPage()
    expect(await screen.findByRole('status')).toHaveTextContent(/hơn 200 item/)
  })

  it('KHÔNG cảnh báo khi đã tải hết', async () => {
    stubItems([item('x')], null)
    renderPage()
    await screen.findByText('x')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('lỗi tải hiện thông báo của server', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () => new Response(JSON.stringify({ title: 'Not Found', status: 404 }), { status: 404 }),
      ),
    )
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent(/404/)
  })
})

describe('ExplorerPage — tạo item', () => {
  it('hộp thoại mở theo URL, không theo state React', async () => {
    stubItems([item('x')])
    renderPage('?new=1')
    // Deep-link và F5 đều phải mở lại hộp thoại: một form đã điền nửa mà biến mất khi
    // tải lại là mất việc.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('KHÔNG mở khi URL không có ?new=1', async () => {
    stubItems([item('x')])
    renderPage()
    await screen.findByText('x')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('bấm Tạo item đưa ?new=1 vào URL', async () => {
    stubItems([item('x')])
    const { router } = renderPage()
    await screen.findByText('x')
    await userEvent.click(screen.getByRole('button', { name: 'Tạo item' }))
    expect(router.state.location.search).toBe('?new=1')
  })

  it('đóng hộp thoại gỡ ?new=1 khỏi URL', async () => {
    stubItems([item('x')])
    const { router } = renderPage('?new=1')
    await userEvent.click(await screen.findByRole('button', { name: 'Huỷ' }))
    expect(router.state.location.search).toBe('')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('ô connection chỉ hiện khi chọn loại connection', async () => {
    stubItems([item('x')])
    renderPage('?new=1')
    await screen.findByRole('dialog')
    expect(screen.queryByLabelText('Secret ref')).not.toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Loại item'), 'connection')
    expect(screen.getByLabelText('Secret ref')).toBeInTheDocument()
    expect(screen.getByLabelText('Host')).toBeInTheDocument()
  })

  it('ô secret_ref KHÔNG bị che như mật khẩu', async () => {
    // Che bằng dấu sao dạy người dùng rằng ô này nhận mật khẩu, và đó đúng là thứ
    // `_check_ref` ở backend phải chặn. Ô này là một THAM CHIẾU tới secret.
    stubItems([item('x')])
    renderPage('?new=1')
    await screen.findByRole('dialog')
    await userEvent.selectOptions(screen.getByLabelText('Loại item'), 'connection')
    expect(screen.getByLabelText('Secret ref')).not.toHaveAttribute('type', 'password')
    expect(screen.getByText(/không phải mật khẩu/i)).toBeInTheDocument()
  })

  it('lỗi 422 của backend gắn vào ĐÚNG ô input', async () => {
    stubItems([item('x')])
    renderPage('?new=1')
    await screen.findByRole('dialog')

    await userEvent.type(screen.getByLabelText('Tên kỹ thuật'), 'Ten Sai')
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({
              title: 'Unprocessable Content',
              status: 422,
              detail: 'dữ liệu gửi lên không hợp lệ',
              errors: [{ loc: ['body', 'name'], msg: 'sai định dạng', type: 'x' }],
            }),
            { status: 422, headers: { 'content-type': 'application/problem+json' } },
          ),
      ),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Tạo' }))

    // Thông báo nằm cạnh ô `name`, không phải một câu chung ở cuối form: với sáu ô thì
    // "dữ liệu không hợp lệ" bắt người dùng tự đoán ô nào sai.
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toContain('sai định dạng')
  })
})

describe('ExplorerPage — đổi tên và xoá trong cây', () => {
  it('đổi tên dùng ETag dựng từ version, không cần GET thêm', async () => {
    const mock = stubItems([{ ...item('x'), version: 7 }])
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Đổi tên' }))

    const input = screen.getByLabelText('Tên hiển thị')
    await userEvent.clear(input)
    await userEvent.type(input, 'Tên mới{Enter}')

    const patch = mock.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'PATCH')
    expect(patch).toBeDefined()
    expect((patch?.[1]?.headers as Record<string, string>)['If-Match']).toBe('W/"7"')
  })

  it('Escape huỷ đổi tên mà KHÔNG gửi gì', async () => {
    const mock = stubItems([item('x')])
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Đổi tên' }))
    await userEvent.type(screen.getByLabelText('Tên hiển thị'), 'nửa đường{Escape}')

    expect(screen.queryByLabelText('Tên hiển thị')).not.toBeInTheDocument()
    expect(mock.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'PATCH')).toBe(false)
  })

  it('xoá cần hai bước, không xoá ngay khi bấm', async () => {
    // Xoá mềm phục hồi được, nhưng một cú bấm lạc tay vẫn làm item biến khỏi cây của
    // cả nhóm cho tới khi có người khôi phục.
    const mock = stubItems([item('x')])
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Xoá' }))
    expect(mock.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE')).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: 'Xác nhận xoá' }))
    expect(mock.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE')).toBe(true)
  })
})

describe('ExplorerPage — tên gọi của control', () => {
  it('bộ lọc và ô loại trong hộp thoại KHÔNG cùng một tên gọi', async () => {
    // Hai control cùng accessible name trên một màn hình làm screen reader đọc ra hai
    // thứ không phân biệt được, và `getByLabelText` cũng không chọn được cái nào.
    stubItems([item('x')])
    renderPage('?new=1')
    await screen.findByRole('dialog')
    expect(screen.getByLabelText('Loại')).toHaveAttribute('id', 'type-filter')
    expect(screen.getByLabelText('Loại item')).toHaveAttribute('id', 'new-type')
  })
})
