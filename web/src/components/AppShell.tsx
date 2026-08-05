import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

export interface CurrentUser {
  subject: string
  email: string
  display_name: string
  // Backend trả trường này từ Task 3 và RBAC theo nhóm chạy thật từ Task 25. Thiếu
  // nó ở đây thì TypeScript không bao giờ nhắc ai rằng nhóm tồn tại, và tính năng
  // nằm im trong khi backend đã sẵn sàng.
  groups: string[]
}

interface AppShellProps {
  user: CurrentUser
  onLogout: () => void
  children?: ReactNode
}

// Bốn mục cũ — Trang chủ, Monitor, Catalog, Admin — BỎ ở Giai đoạn 1: chúng chưa
// có trang, và một mục nav dẫn tới trang trắng tệ hơn là không có mục đó. Monitor và
// Catalog quay lại ở Giai đoạn 3, Admin ở Giai đoạn 4.
const NAV_ITEMS = [{ label: 'Workspace', glyph: '📊', to: '/' }] as const

export function AppShell({ user, onLogout, children }: AppShellProps) {
  return (
    <div className="flex h-full flex-col bg-surface text-ink">
      <header className="flex h-12 shrink-0 items-center gap-4 border-b border-line px-4">
        <span className="font-semibold tracking-tight">Loom</span>
        <span className="text-sm text-dim">Chưa chọn workspace</span>
        <div className="flex-1" />
        {/* Giai đoạn 0 đặt `aria-hidden` vì palette chưa tồn tại — đọc "⌘K" lúc đó là
            hứa một tính năng không có. Task 31 làm nó có thật, nên giữ `aria-hidden`
            bây giờ là ẩn một tính năng đang chạy khỏi screen reader. */}
        <kbd className="rounded border border-line px-2 py-0.5 text-xs text-dim">⌘K</kbd>
        <span className="text-sm">{user.display_name}</span>
        <button
          type="button"
          onClick={onLogout}
          className="rounded px-2 py-1 text-sm text-dim hover:bg-muted"
        >
          Đăng xuất
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <nav
          aria-label="Điều hướng chính"
          className="flex w-14 shrink-0 flex-col items-center gap-1 border-r border-line py-3"
        >
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.label}
              to={item.to}
              aria-label={item.label}
              title={item.label}
              className={({ isActive }) =>
                `flex h-10 w-10 items-center justify-center rounded ${
                  isActive ? 'bg-muted' : 'hover:bg-muted'
                }`
              }
            >
              <span aria-hidden>{item.glyph}</span>
            </NavLink>
          ))}
        </nav>

        {/* Trạng thái rỗng "Giai đoạn 0 hoàn tất" đã bỏ: với router thì children luôn
            là <Outlet />, nên nó không còn đường nào tới được — và nội dung của nó
            giờ cũng sai. Một mảng UI chết mang chữ lỗi thời tệ hơn là không có. */}
        <main className="min-w-0 flex-1 overflow-auto p-8">{children}</main>
      </div>
    </div>
  )
}
