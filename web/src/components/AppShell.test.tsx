import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AppShell } from './AppShell'

const user = { subject: 'CgRsb25n', email: 'long@loom.local', display_name: 'Long' }

describe('AppShell', () => {
  it('hiển thị tên sản phẩm', () => {
    render(<AppShell user={user} onLogout={vi.fn()} />)
    expect(screen.getByText('Loom')).toBeInTheDocument()
  })

  it('hiển thị tên người dùng đang đăng nhập', () => {
    render(<AppShell user={user} onLogout={vi.fn()} />)
    expect(screen.getByText('Long')).toBeInTheDocument()
  })

  it('có đủ các mục điều hướng của Giai đoạn 0', () => {
    render(<AppShell user={user} onLogout={vi.fn()} />)
    for (const label of ['Trang chủ', 'Workspace', 'Monitor', 'Catalog', 'Admin']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
  })

  it('hiển thị trạng thái rỗng nói rõ bước tiếp theo', () => {
    render(<AppShell user={user} onLogout={vi.fn()} />)
    expect(screen.getByTestId('empty-state')).toHaveTextContent(/Giai đoạn 1/)
  })

  it('gọi onLogout khi bấm Đăng xuất', async () => {
    const onLogout = vi.fn()
    render(<AppShell user={user} onLogout={onLogout} />)
    await userEvent.click(screen.getByRole('button', { name: 'Đăng xuất' }))
    expect(onLogout).toHaveBeenCalledOnce()
  })

  it('hiển thị children khi được truyền vào', () => {
    render(
      <AppShell user={user} onLogout={vi.fn()}>
        <p>nội dung thật</p>
      </AppShell>,
    )
    expect(screen.getByText('nội dung thật')).toBeInTheDocument()
    expect(screen.queryByTestId('empty-state')).not.toBeInTheDocument()
  })
})
