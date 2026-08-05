import { Outlet } from 'react-router'

import { apiPost } from '../lib/api'
import { navigateTo } from '../lib/navigate'
import { useCurrentUser } from '../lib/useCurrentUser'
import { AppShell } from './AppShell'
import { CommandPalette } from './CommandPalette'

/**
 * Nối `AppShell` với router.
 *
 * `AppShell` nhận danh tính và hành vi đăng xuất qua PROPS chứ không tự gọi hook —
 * nó vẫn cần Router context vì nav là `NavLink`, nhưng nó không biết gì về việc dữ
 * liệu tới từ đâu, nên test nó không phải giả lập `useCurrentUser` hay `fetch`.
 *
 * `CommandPalette` vì vậy nằm ở ĐÂY, không trong `AppShell`: nó gọi `useSearch`, và đặt
 * nó vào `AppShell` sẽ buộc mọi test của shell phải dựng một `QueryClient` — tức xoá
 * đúng ranh giới vừa nói ở trên. Ở đây thì ⌘K vẫn chạy từ mọi màn hình, và chỉ có MỘT
 * listener keydown chứ không phải một bản sao trên mỗi trang.
 */
export function AppLayout() {
  const { data: user } = useCurrentUser()
  // `App.tsx` đã xử lý loading và 401 trước khi router được render, nên tới đây
  // `user` gần như luôn có. Trả null thay vì `!` để một thay đổi ở App.tsx sau này
  // không biến thành lỗi runtime.
  if (!user) return null

  return (
    <AppShell
      user={user}
      onLogout={async () => {
        await apiPost('/api/v1/auth/logout')
        navigateTo('/api/v1/auth/login')
      }}
    >
      <Outlet />
      <CommandPalette />
    </AppShell>
  )
}
