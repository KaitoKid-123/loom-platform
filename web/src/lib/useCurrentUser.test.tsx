import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useCurrentUser } from './useCurrentUser'

/** Một QueryClient cho mỗi test, ổn định qua các lần render lại.
 *  retryDelay: 0 để không phải chờ backoff thật. */
function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useCurrentUser retry', () => {
  it('401: gọi đúng MỘT lần, không thử lại', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useCurrentUser(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('500: gọi đúng BA lần (1 + 2 lần thử lại) rồi báo lỗi', async () => {
    // Đếm chứ không đọc code: predicate cũ bỏ qua failureCount nên thử lại vô
    // hạn, và không test nào phát hiện được vì không ai đếm số lần gọi.
    const fetchMock = vi.fn(async () => new Response('boom', { status: 500 }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useCurrentUser(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 })

    expect(fetchMock).toHaveBeenCalledTimes(3)
  })
})
