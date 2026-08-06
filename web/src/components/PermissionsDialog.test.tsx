import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { countAdmins } from '../lib/useRoles'
import { PermissionsDialog } from './PermissionsDialog'

const WS = '11111111-1111-1111-1111-111111111111'
const USER = '22222222-2222-2222-2222-222222222222'

const ADMIN_USER = { principal_type: 'user' as const, user_id: 'u1', group: null, role: 'admin' }
const ADMIN_GROUP = {
  principal_type: 'group' as const,
  user_id: null,
  group: 'ops',
  role: 'admin',
}

function payload(grantable: string[], items: unknown[] = []) {
  return new Response(JSON.stringify({ items, grantable_roles: grantable }), { status: 200 })
}

function renderDialog(onClose = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <PermissionsDialog scopeType="workspaces" scopeId={WS} onClose={onClose} />
    </QueryClientProvider>,
  )
}

function stubRoles(grantable: string[], items: unknown[] = []) {
  const mock = vi.fn<typeof fetch>(async () => payload(grantable, items))
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PermissionsDialog', () => {
  it('member KHÔNG thấy tuỳ chọn admin', async () => {
    stubRoles(['viewer', 'contributor'])
    renderDialog()
    const select = await screen.findByLabelText(/role/i)
    const options = Array.from(select.querySelectorAll('option'))
      .map((o) => o.textContent)
      .filter((t) => t !== '—')
    expect(options).toEqual(['viewer', 'contributor'])
  })

  it('admin thấy đủ bốn vai trò', async () => {
    // Vế đối: không có nó, một bản cài đặt trả danh sách rỗng vẫn thoả test trên.
    stubRoles(['viewer', 'contributor', 'member', 'admin'])
    renderDialog()
    const select = await screen.findByLabelText(/role/i)
    const options = Array.from(select.querySelectorAll('option'))
      .map((o) => o.textContent)
      .filter((t) => t !== '—')
    expect(options).toEqual(['viewer', 'contributor', 'member', 'admin'])
  })

  it('nút thu bị vô hiệu KÈM LÝ DO khi là admin cuối cùng', async () => {
    stubRoles(['viewer', 'contributor', 'member', 'admin'], [ADMIN_USER])
    renderDialog()
    const button = await screen.findByRole('button', { name: /remove/i })
    expect(button).toBeDisabled()
    // Vô hiệu mà không nói lý do tệ hơn không vô hiệu: người dùng bấm mãi không được và
    // không biết vì sao.
    expect(button).toHaveAccessibleDescription(/last admin/i)
  })

  it('nút thu bật lại khi có admin thứ hai LÀ MỘT NHÓM', async () => {
    // Chỉ đếm người thì nút này vẫn bị vô hiệu oan, và không ai gỡ được admin nào cả.
    stubRoles(['viewer', 'contributor', 'member', 'admin'], [ADMIN_USER, ADMIN_GROUP])
    renderDialog()
    const buttons = await screen.findAllByRole('button', { name: /remove/i })
    expect(buttons[0]).toBeEnabled()
    expect(buttons[0]).not.toHaveAccessibleDescription(/last admin/i)
  })

  it('viewer KHÔNG phải admin cuối cùng — nút thu của họ vẫn bật', async () => {
    // Chỉ kiểm `adminCount <= 1` mà bỏ vế `row.role === 'admin'` sẽ vô hiệu nút của MỌI
    // hàng khi phạm vi còn một admin.
    stubRoles(
      ['viewer', 'contributor', 'member', 'admin'],
      [ADMIN_USER, { principal_type: 'user', user_id: 'u2', group: null, role: 'viewer' }],
    )
    renderDialog()
    const buttons = await screen.findAllByRole('button', { name: /remove/i })
    expect(buttons[0]).toBeDisabled()
    expect(buttons[1]).toBeEnabled()
  })

  it('403 từ server vẫn hiện thông báo, không im lặng', async () => {
    // Đây là phép kiểm rằng LỚP THỨ NHẤT vẫn hoạt động. Hai thứ ở trên chỉ là lớp thứ
    // hai: gỡ chúng thì UI xấu, còn gỡ kiểm ở `RoleStore` thì ai gọi API trực tiếp cũng
    // leo thang được.
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async () => {
        call += 1
        if (call === 1) return payload(['viewer'])
        return new Response(
          JSON.stringify({
            title: 'Forbidden',
            status: 403,
            detail: 'vai trò member không gán được vai trò admin',
          }),
          { status: 403, headers: { 'content-type': 'application/problem+json' } },
        )
      }),
    )
    renderDialog()
    await screen.findByLabelText(/role/i)
    await userEvent.type(screen.getByLabelText(/user or group/i), 'ops')
    await userEvent.selectOptions(screen.getByLabelText(/role/i), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(screen.getByRole('alert')).// Nguyên văn câu của SERVER, và câu đó vẫn tiếng Việt: nhãn giao diện đổi sang
    // tiếng Anh không kéo theo thông báo lỗi backend.
      toHaveTextContent(/không gán được/i))
  })

  it('409 admin cuối cùng từ server vẫn hiện, kể cả khi UI đã cố chặn', async () => {
    // UI vô hiệu nút, nhưng nếu ai đó lách được (dữ liệu cũ, hai tab) thì câu của server
    // phải tới người dùng — nó nói phải làm gì tiếp.
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async () => {
        call += 1
        if (call === 1) return payload(['admin'], [ADMIN_USER, ADMIN_GROUP])
        return new Response(
          JSON.stringify({
            title: 'Conflict',
            status: 409,
            detail: 'đây là admin cuối cùng của phạm vi này — gán admin khác trước khi thu',
          }),
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        )
      }),
    )
    renderDialog()
    const buttons = await screen.findAllByRole('button', { name: /remove/i })
    await userEvent.click(buttons[0])
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/gán admin khác/i))
  })

  it('UUID gán cho NGƯỜI DÙNG, chuỗi khác gán cho NHÓM', async () => {
    // Backend đòi đúng một trong `user_id`/`group` và trả 422 nếu gửi cả hai hoặc không
    // gửi gì, nên việc chọn phải xảy ra ở đây.
    const mock = stubRoles(['viewer'])
    renderDialog()
    await screen.findByLabelText(/role/i)
    await userEvent.selectOptions(screen.getByLabelText(/role/i), 'viewer')

    // Lọc theo METHOD: `onSuccess` nạp lại danh sách, nên lần gọi cuối cùng là một GET
    // không có body — `calls.at(-1)` sẽ đọc nhầm request đó.
    const puts = () => mock.mock.calls.filter((c) => c[1]?.method === 'PUT')

    await userEvent.type(screen.getByLabelText(/user or group/i), USER)
    await userEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(JSON.parse(String(puts()[0][1]?.body))).toEqual({ role: 'viewer', user_id: USER })

    await userEvent.type(screen.getByLabelText(/user or group/i), 'data-eng')
    await userEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(puts()).toHaveLength(2))
    expect(JSON.parse(String(puts()[1][1]?.body))).toEqual({ role: 'viewer', group: 'data-eng' })
  })

  it('thu quyền gửi principal qua QUERY, không qua body DELETE', async () => {
    // RFC 9110 nói client không nên gửi nội dung trong DELETE, và một lệnh thu bị gateway
    // lược body là yêu cầu thiếu đúng phần nói THU CỦA AI.
    const mock = stubRoles(['admin'], [ADMIN_USER, ADMIN_GROUP])
    renderDialog()
    const buttons = await screen.findAllByRole('button', { name: /remove/i })
    await userEvent.click(buttons[1])
    await waitFor(() => expect(mock.mock.calls.length).toBeGreaterThan(1))
    const [url, init] = mock.mock.calls.find((c) => c[1]?.method === 'DELETE') ?? []
    expect(String(url)).toContain('group=ops')
    expect(init?.body).toBeUndefined()
  })

  it('nút Gán bị vô hiệu khi thiếu principal hoặc vai trò', async () => {
    stubRoles(['viewer'])
    renderDialog()
    await screen.findByLabelText(/role/i)
    expect(screen.getByRole('button', { name: 'Grant' })).toBeDisabled()

    await userEvent.type(screen.getByLabelText(/user or group/i), 'ops')
    expect(screen.getByRole('button', { name: 'Grant' })).toBeDisabled()

    await userEvent.selectOptions(screen.getByLabelText(/role/i), 'viewer')
    expect(screen.getByRole('button', { name: 'Grant' })).toBeEnabled()
  })

  it('403 khi ĐỌC danh sách quyền hiện thông báo thay vì bảng rỗng', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          new Response(JSON.stringify({ title: 'Forbidden', status: 403 }), { status: 403 }),
      ),
    )
    renderDialog()
    expect(await screen.findByRole('alert')).toHaveTextContent(/403|Forbidden/)
  })
})

describe('countAdmins', () => {
  it('tính cả nhóm, giống backend', () => {
    expect(countAdmins([ADMIN_USER, ADMIN_GROUP])).toBe(2)
    expect(countAdmins([ADMIN_GROUP])).toBe(1)
    expect(
      countAdmins([{ principal_type: 'user', user_id: 'u', group: null, role: 'member' }]),
    ).toBe(0)
  })
})
