import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

export interface CurrentUser {
  subject: string
  email: string
  display_name: string
  // Backend trả trường này từ Task 3 và RBAC theo nhóm chạy thật từ Task 25. Thiếu nó ở
  // đây thì TypeScript không bao giờ nhắc ai rằng nhóm tồn tại, và tính năng nằm im.
  groups: string[]
}

/** Sự kiện mở bảng lệnh. Xem `CommandPalette` để biết vì sao là window event. */
export const OPEN_PALETTE_EVENT = 'loom:open-palette'

interface AppShellProps {
  user: CurrentUser
  onLogout: () => void
  /** Panel ngữ cảnh bên trái. Vắng mặt khi chưa vào workspace nào. */
  sidebar?: ReactNode
  children?: ReactNode
}

// Home, Monitor và Catalog vẫn chưa có trang nên chưa vào rail: một icon dẫn tới trang
// trắng tệ hơn là không có icon đó. Monitor và Catalog quay lại ở Giai đoạn 3.
const NAV_ITEMS = [
  {
    label: 'Workspaces',
    to: '/',
    end: true,
    // Bốn ô vuông — ngăn chứa công việc.
    d: 'M2.5 2.5h4.2v4.2H2.5zM9.3 2.5h4.2v4.2H9.3zM2.5 9.3h4.2v4.2H2.5zM9.3 9.3h4.2v4.2H9.3z',
  },
  {
    label: 'Domains',
    to: '/domains',
    end: false,
    // Một nút toả ra ba nhánh — lĩnh vực nghiệp vụ chứa nhiều workspace.
    d: 'M8 2.2v3.4M8 5.6 3.4 9.2v4.6M8 5.6l4.6 3.6v4.6M8 5.6v8.2',
  },
] as const

/**
 * Chữ cái đầu cho ô đại diện.
 *
 * Nhận `string | undefined` và không bao giờ ném: đây là một hàm TRANG TRÍ, và một
 * `display_name` thiếu không được phép hạ cả vỏ ứng dụng xuống trang lỗi của React
 * Router. Đã gặp thật — `name.split` trên `undefined` làm trắng toàn bộ màn hình.
 */
function initials(name: string | undefined): string {
  return (
    (name ?? '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase() ?? '')
      .join('') || '?'
  )
}

export function AppShell({ user, onLogout, sidebar, children }: AppShellProps) {
  return (
    <div className="flex h-full flex-col bg-canvas text-ink">
      {/* Dải header màu đậm: nó cho ứng dụng một bản sắc, và nó tách phần điều hướng
          toàn cục khỏi vùng nội dung rõ hơn bất kỳ đường kẻ nào. */}
      <header className="flex h-12 shrink-0 items-center gap-3 bg-[#10312f] px-3 text-white">
        <span className="px-1 text-[15px] font-semibold tracking-tight">Loom</span>

        {/* Ô tìm kiếm ở GIỮA, như Fabric. Là một `button` chứ không `input`: gõ vào đây
            phải mở bảng lệnh, và một input thật sẽ nhận phím rồi nuốt mất chúng trước
            khi ô của bảng lệnh kịp lấy nét. */}
        <div className="flex flex-1 justify-center">
          <button
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent(OPEN_PALETTE_EVENT))}
            className="flex h-7 w-full max-w-md items-center gap-2 rounded-md bg-white/12 px-2.5 text-left text-[13px] text-white/70 transition-colors hover:bg-white/20"
          >
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
              <circle cx="7" cy="7" r="4.4" stroke="currentColor" strokeWidth="1.4" />
              <path
                d="m10.4 10.4 3 3"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
            <span className="flex-1">Search Loom</span>
            {/* Giai đoạn 0 đặt `aria-hidden` lên gợi ý này vì palette chưa tồn tại. Task
                31 làm nó có thật, nên giữ `aria-hidden` là ẩn một tính năng đang chạy
                khỏi screen reader. */}
            <kbd className="rounded border border-white/25 px-1.5 py-px font-sans text-[11px] text-white/60">
              ⌘K
            </kbd>
          </button>
        </div>

        <div className="flex items-center gap-2">
          <span className="hidden text-[13px] text-white/80 sm:inline">{user.display_name}</span>
          {/* Chữ cái đầu chứ không ảnh đại diện: Loom không có ảnh, và một ô xám trống
              trông như ảnh vừa tải hỏng. */}
          <span
            aria-hidden
            className="flex h-7 w-7 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-white"
          >
            {initials(user.display_name)}
          </span>
          <button
            type="button"
            onClick={onLogout}
            className="rounded px-2 py-1 text-[13px] text-white/75 transition-colors hover:bg-white/12 hover:text-white"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <nav
          aria-label="Global"
          className="flex w-12 shrink-0 flex-col items-center gap-1 border-r border-line bg-surface py-2"
        >
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.label}
              to={item.to}
              // `end` cho "Workspaces": không có nó, mục này sáng ở MỌI đường vì `/` là
              // tiền tố của tất cả, và hai mục cùng sáng một lúc.
              end={item.end}
              aria-label={item.label}
              title={item.label}
              className={({ isActive }) =>
                `relative flex h-9 w-9 items-center justify-center rounded transition-colors ${
                  isActive ? 'bg-accent-soft text-accent' : 'text-dim hover:bg-hover'
                }`
              }
            >
              {({ isActive }) => (
                <>
                  {/* Thanh chỉ báo bên trái, không chỉ đổi nền: nền nhạt một mình khó
                      thấy trên màn hình kém tương phản, còn vạch 2px thì luôn thấy. */}
                  {isActive && (
                    <span aria-hidden className="absolute -left-2 h-5 w-[2px] rounded-r bg-accent" />
                  )}
                  <svg width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden>
                    <path
                      d={item.d}
                      stroke="currentColor"
                      strokeWidth="1.4"
                      strokeLinejoin="round"
                    />
                  </svg>
                </>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Render THẲNG, không bọc trong `<aside>` ở đây: `sidebar` là một phần tử JSX
            nên nó LUÔN truthy, và `{sidebar && …}` vẫn dựng một cột trống rộng 224px ở
            những trang không có panel. Component panel tự dựng `<aside>` của nó và trả
            `null` khi không có gì để hiện — đó là chỗ duy nhất biết được điều đó. */}
        {sidebar}

        <main className="min-w-0 flex-1 overflow-auto">{children}</main>
      </div>
    </div>
  )
}
