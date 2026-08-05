import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSearch } from './useSearch'

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function stubOk(items: unknown[] = []) {
  const mock = vi.fn<typeof fetch>(
    async () => new Response(JSON.stringify({ items }), { status: 200 }),
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useSearch', () => {
  it.each(['', '   '])('query rỗng (%j) KHÔNG gọi server', async (term) => {
    const mock = stubOk()
    renderHook(() => useSearch(term), { wrapper: wrapper() })
    await new Promise((r) => setTimeout(r, 30))
    // Không có phép kiểm này thì mỗi lần bấm ⌘K là một round trip cho một câu trả lời
    // mà backend đã cố ý trả rỗng.
    expect(mock).not.toHaveBeenCalled()
  })

  it('truyền AbortSignal xuống fetch', async () => {
    const mock = stubOk()
    const { result } = renderHook(() => useSearch('abc'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal)
  })

  it('mã hoá query để ký tự đặc biệt không phá URL', async () => {
    // `q=a&b=c` nối chuỗi sẽ tách thành hai tham số và server nhận `q=a`: người dùng
    // tìm một thứ, hệ thống tìm thứ khác, không lỗi nào báo.
    const mock = stubOk()
    const { result } = renderHook(() => useSearch('a&b=c d'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const url = String(mock.mock.calls[0][0])
    expect(url).toContain('a%26b%3Dc')
    expect(url.split('q=')[1]).not.toContain('&')
  })

  it('gõ tiếp huỷ request trước — kết quả cũ không ghi đè kết quả mới', async () => {
    // Đây là phép kiểm quan trọng nhất của hook: phép kiểm ở trên chỉ nói signal ĐƯỢC
    // TRUYỀN, không nói nó có tác dụng.
    const aborted: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input, init) => {
        const url = String(input)
        // Query CŨ chậm hơn query mới. Không huỷ thì nó về sau và ghi đè.
        const slow = url.includes('q=ab&') || url.endsWith('q=ab')
        const id = slow ? 'cu' : 'moi'
        return new Promise<Response>((resolve, reject) => {
          const timer = setTimeout(
            () => resolve(new Response(JSON.stringify({ items: [{ id }] }), { status: 200 })),
            slow ? 60 : 5,
          )
          init?.signal?.addEventListener('abort', () => {
            clearTimeout(timer)
            aborted.push(id)
            reject(new DOMException('aborted', 'AbortError'))
          })
        })
      }),
    )

    const { result, rerender } = renderHook(({ q }: { q: string }) => useSearch(q), {
      wrapper: wrapper(),
      initialProps: { q: 'ab' },
    })
    rerender({ q: 'abc' })

    await waitFor(() => expect(result.current.data?.items[0]?.id).toBe('moi'))
    // Chờ quá thời điểm query chậm ĐÁNG LẼ trả về, rồi khẳng định lại: không huỷ thì
    // đúng lúc này giá trị đã bị ghi đè thành 'cu'.
    await new Promise((r) => setTimeout(r, 90))
    expect(result.current.data?.items[0]?.id).toBe('moi')
  })

  it('kết quả của hai term khác nhau không lẫn cache', async () => {
    const mock = vi.fn<typeof fetch>(async (input) => {
      const term = new URL(String(input), 'http://t').searchParams.get('q')
      return new Response(JSON.stringify({ items: [{ id: term }] }), { status: 200 })
    })
    vi.stubGlobal('fetch', mock)

    const { result, rerender } = renderHook(({ q }: { q: string }) => useSearch(q), {
      wrapper: wrapper(),
      initialProps: { q: 'mot' },
    })
    await waitFor(() => expect(result.current.data?.items[0]?.id).toBe('mot'))
    rerender({ q: 'hai' })
    await waitFor(() => expect(result.current.data?.items[0]?.id).toBe('hai'))
  })
})
