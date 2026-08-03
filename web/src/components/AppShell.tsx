import type { ReactNode } from 'react'

export interface CurrentUser {
  subject: string
  email: string
  display_name: string
}

interface AppShellProps {
  user: CurrentUser
  onLogout: () => void
  children?: ReactNode
}

const NAV_ITEMS = [
  { label: 'Trang chủ', glyph: '🏠' },
  { label: 'Workspace', glyph: '📊' },
  { label: 'Monitor', glyph: '⚡' },
  { label: 'Catalog', glyph: '🗂' },
  { label: 'Admin', glyph: '⚙' },
] as const

export function AppShell({ user, onLogout, children }: AppShellProps) {
  return (
    <div className="flex h-full flex-col bg-surface text-ink">
      <header className="flex h-12 shrink-0 items-center gap-4 border-b border-line px-4">
        <span className="font-semibold tracking-tight">Loom</span>
        <span className="text-sm text-dim">Chưa chọn workspace</span>
        <div className="flex-1" />
        {/* aria-hidden: Giai đoạn 0 chưa có command palette. Để screen reader
            đọc "⌘K" là hứa một tính năng chưa tồn tại. Bỏ aria-hidden khi
            palette thật ra đời. */}
        <kbd aria-hidden className="rounded border border-line px-2 py-0.5 text-xs text-dim">
          ⌘K
        </kbd>
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
            <a
              key={item.label}
              href="#"
              aria-label={item.label}
              title={item.label}
              className="flex h-10 w-10 items-center justify-center rounded hover:bg-muted"
            >
              <span aria-hidden>{item.glyph}</span>
            </a>
          ))}
        </nav>

        <main className="min-w-0 flex-1 overflow-auto p-8">
          {children ?? (
            <div
              data-testid="empty-state"
              className="mx-auto max-w-lg rounded-lg border border-dashed border-line p-8 text-center"
            >
              <h1 className="text-lg font-medium">Nền tảng đã chạy</h1>
              <p className="mt-2 text-sm text-dim">
                Giai đoạn 0 hoàn tất. Workspace và item sẽ xuất hiện ở Giai đoạn 1.
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
