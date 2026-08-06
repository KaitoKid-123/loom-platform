import { useEffect } from 'react'
import { RouterProvider, createBrowserRouter } from 'react-router'

import { navigateTo } from './lib/navigate'
import { UnauthorizedError, useCurrentUser } from './lib/useCurrentUser'
import { routeObjects } from './routes'

const LOGIN_URL = '/api/v1/auth/login'

// Dựng MỘT LẦN ở tầng module, không trong thân component: `createBrowserRouter` gắn
// listener vào history, và dựng lại nó mỗi lần render sẽ mất vị trí điều hướng cùng
// mọi state của router.
const router = createBrowserRouter(routeObjects)

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
        <p className="text-[14px]">Sign-in failed.</p>
        <a href={LOGIN_URL} className="text-accent underline">
          Try again
        </a>
      </div>
    )
  }

  if (isPending || needsLogin) {
    return (
      <div role="status" className="flex h-full items-center justify-center text-sm">
        Loading…
      </div>
    )
  }

  if (error || !user) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-8 text-sm">
        Cannot reach the Loom server. Check `kubectl -n loom get pods`.
      </div>
    )
  }

  return <RouterProvider router={router} />
}
