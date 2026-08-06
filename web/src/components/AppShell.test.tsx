import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { AppShell } from './AppShell'

const user = {
  subject: 'CgRsb25n',
  email: 'long@loom.local',
  display_name: 'Long',
  groups: ['data-eng'],
}

// Nav là `NavLink`, và `NavLink` cần Router context — nav VỐN là chuyện routing, nên
// bọc ở đây thay vì bẻ AppShell thành nhận nav qua props chỉ để test khỏi cần router.
function renderShell(children?: ReactNode) {
  return render(
    <MemoryRouter>
      <AppShell user={user} onLogout={vi.fn()}>
        {children}
      </AppShell>
    </MemoryRouter>,
  )
}

describe('AppShell', () => {
  it('hiển thị tên sản phẩm', () => {
    renderShell()
    expect(screen.getByText('Loom')).toBeInTheDocument()
  })

  it('hiển thị tên người dùng đang đăng nhập', () => {
    renderShell()
    expect(screen.getByText('Long')).toBeInTheDocument()
  })

  it('nav chỉ có những mục ĐÃ có trang', () => {
    renderShell()
    expect(screen.getByRole('link', { name: 'Workspaces' })).toBeInTheDocument()
    // Bốn mục cũ bị bỏ có chủ đích: một mục nav dẫn tới trang trắng tệ hơn là không
    // có mục đó. Khẳng định chúng KHÔNG còn, để ai thêm lại phải thêm cả trang.
    for (const label of ['Home', 'Monitor', 'Catalog', 'Admin']) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
    }
  })

  it('gọi onLogout khi bấm Đăng xuất', async () => {
    const onLogout = vi.fn()
    render(
      <MemoryRouter>
        <AppShell user={user} onLogout={onLogout} />
      </MemoryRouter>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(onLogout).toHaveBeenCalledOnce()
  })

  it('hiển thị children khi được truyền vào', () => {
    renderShell(<p>nội dung thật</p>)
    expect(screen.getByText('nội dung thật')).toBeInTheDocument()
  })
})

describe('AppShell — gợi ý ⌘K', () => {
  it('KHÔNG còn aria-hidden: palette đã có thật từ Task 31', () => {
    // Giai đoạn 0 đặt aria-hidden vì palette chưa tồn tại. Giữ nó sau khi palette chạy
    // là ẩn một tính năng đang hoạt động khỏi screen reader.
    renderShell()
    const hint = screen.getByText('⌘K')
    expect(hint).not.toHaveAttribute('aria-hidden')
  })
})
