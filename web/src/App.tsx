import { useEffect } from 'react'

import { AppShell } from './components/AppShell'
import { apiPost } from './lib/api'
import { navigateTo } from './lib/navigate'
import { UnauthorizedError, useCurrentUser } from './lib/useCurrentUser'

const LOGIN_URL = '/api/v1/auth/login'

export function App() {
  const { data: user, error, isPending } = useCurrentUser()
  const needsLogin = error instanceof UnauthorizedError

  // `/auth/callback` chuyển hướng về `/?error=login_failed` khi đăng nhập hỏng.
  // Không đọc cờ này thì thành vòng lặp câm: 401 → tự sang /auth/login → Dex
  // còn phiên SSO nên duyệt lại không hỏi gì → callback hỏng y hệt → quay vòng.
  // Nguyên nhân thoáng qua thì tự khỏi; nguyên nhân dai dẳng (sai client_secret,
  // pod không tới được Dex, lệch đồng hồ) thì quay mãi mà không hiện lỗi nào.
  const loginFailed = new URLSearchParams(window.location.search).has('error')

  useEffect(() => {
    if (needsLogin && !loginFailed) {
      navigateTo(LOGIN_URL)
    }
  }, [needsLogin, loginFailed])

  if (needsLogin && loginFailed) {
    return (
      <div role="alert" className="flex h-full flex-col items-center justify-center gap-3 text-sm">
        <p>Đăng nhập không thành công.</p>
        <a href={LOGIN_URL} className="text-accent underline">
          Thử lại
        </a>
      </div>
    )
  }

  if (isPending || needsLogin) {
    return (
      <div role="status" className="flex h-full items-center justify-center text-sm">
        Đang tải…
      </div>
    )
  }

  if (error || !user) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-8 text-sm">
        Không kết nối được tới máy chủ Loom. Kiểm tra `kubectl -n loom get pods`.
      </div>
    )
  }

  const logout = async () => {
    await apiPost('/api/v1/auth/logout')
    navigateTo(LOGIN_URL)
  }

  return <AppShell user={user} onLogout={logout} />
}
