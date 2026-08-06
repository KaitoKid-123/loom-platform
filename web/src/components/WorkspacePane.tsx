import { NavLink, useMatch } from 'react-router'

import { useWorkspaces } from '../lib/useWorkspaces'

/**
 * Panel ngữ cảnh của workspace đang mở.
 *
 * `useMatch` chứ không `useParams`: đây là một layout, và `useParams` trong layout chỉ
 * trả tham số của CHÍNH route layout đó — nó không thấy `:workspaceId` nằm ở route con.
 * Đó đúng là lý do header cũ ghi "Chưa chọn workspace" mãi mãi.
 *
 * Trả `null` khi không ở trong workspace nào, và `AppShell` bỏ hẳn cột khi không có
 * panel — một cột trống rộng 224px là một khoảng vô nghĩa chiếm chỗ.
 */
export function WorkspacePane() {
  const match = useMatch('/workspaces/:workspaceId/*')
  const workspaceId = match?.params.workspaceId
  const { data } = useWorkspaces()

  if (!workspaceId) return null

  // `?.items?.` chứ không `?.items.`: một phản hồi thiếu mảng `items` làm `.find` ném,
  // và vì panel này nằm trong vỏ ứng dụng thì React Router thay CẢ MÀN HÌNH bằng trang
  // lỗi của nó — người dùng mất luôn header, rail và trang họ đang xem. Cùng lỗi đã gặp
  // ở danh sách version trong `ItemPage`.
  const workspace = data?.items?.find((w) => w.id === workspaceId)

  return (
    <aside className="w-56 shrink-0 overflow-y-auto border-r border-line bg-surface py-3">
      <div className="px-3 pb-3">
        <p className="text-[11px] font-medium uppercase tracking-wider text-faint">Workspace</p>
        <h2 className="mt-1 truncate text-[13px] font-semibold" title={workspace?.display_name}>
          {/* Trong lúc danh sách workspace còn đang tải thì chưa biết tên. Hiện một ô
              xám bằng đúng kích thước chữ, không hiện "Đang tải…" — chữ đó dài hơn tên
              thật và cả panel giật một nhịp khi tên về. */}
          {workspace?.display_name ?? (
            <span aria-hidden className="block h-4 w-28 animate-pulse rounded bg-hover" />
          )}
        </h2>
        {workspace && (
          <span className="mt-1.5 inline-block rounded bg-raised px-1.5 py-0.5 text-[11px] text-dim">
            {workspace.my_role}
          </span>
        )}
      </div>

      <nav aria-label="Workspace" className="border-t border-line pt-2">
        <PaneLink to={`/workspaces/${workspaceId}/items`} end>
          All items
        </PaneLink>
        <PaneLink to={`/workspaces/${workspaceId}/connections`}>Connections</PaneLink>
      </nav>
    </aside>
  )
}

function PaneLink({
  to,
  end,
  children,
}: {
  to: string
  end?: boolean
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      // `end` cho "All items": không có nó, mục này vẫn sáng khi đang xem một item con
      // hoặc trang Connections, vì cả hai đường đều bắt đầu bằng cùng tiền tố.
      end={end}
      className={({ isActive }) =>
        `relative block px-3 py-1.5 text-[13px] transition-colors ${
          isActive
            ? 'bg-selected font-medium text-accent'
            : 'text-ink hover:bg-hover'
        }`
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <span aria-hidden className="absolute left-0 top-1 bottom-1 w-[2px] bg-accent" />
          )}
          {children}
        </>
      )}
    </NavLink>
  )
}
