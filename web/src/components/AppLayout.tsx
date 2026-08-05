import { Outlet } from 'react-router'

import { apiPost } from '../lib/api'
import { navigateTo } from '../lib/navigate'
import { useCurrentUser } from '../lib/useCurrentUser'
import { AppShell } from './AppShell'

/**
 * Nối `AppShell` với router.
 *
 * `AppShell` nhận danh tính và hành vi đăng xuất qua PROPS chứ không tự gọi hook —
 * nó vẫn cần Router context vì nav là `NavLink`, nhưng nó không biết gì về việc dữ
 * liệu tới từ đâu, nên test nó không phải giả lập `useCurrentUser` hay `fetch`.
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
    </AppShell>
  )
}
