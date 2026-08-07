import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useRunQuery } from './useRunQuery'

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** Backend giả điều khiển ĐƯỢC bằng tay — mỗi `GET /query/{id}` trả PHẦN TỬ ĐẦU của
 * hàng đợi đã xếp cho `id` đó, hoặc `running` nếu hàng đợi rỗng. Test tự quyết định lúc
 * nào query "xong" bằng cách xếp trạng thái NGAY TRƯỚC khi để hook poll tới nó — đúng
 * cách phơi bày race thật giữa "server đã xong" và "client vừa hỏi". */
function fakeBackend() {
  const queues = new Map<string, unknown[]>()
  const calls: Array<{ method: string; url: string }> = []
  let nextId = 0

  function queue(queryId: string, ...statuses: unknown[]) {
    queues.set(queryId, statuses)
  }

  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url })

    if (method === 'POST' && url === '/api/v1/query') {
      const queryId = `q${++nextId}`
      queues.set(queryId, [])
      return jsonResponse(202, { query_id: queryId })
    }
    const match = /\/api\/v1\/query\/(.+)$/.exec(url)
    const queryId = match![1]!
    if (method === 'DELETE') {
      return new Response(null, { status: 202 })
    }
    // GET — phần tử kế tiếp của hàng đợi, giữ nguyên phần tử cuối nếu hàng đợi đã cạn
    // (mô phỏng "vẫn đang chạy" khi test chưa xếp trạng thái nào tiếp theo).
    const remaining = queues.get(queryId) ?? []
    const status = remaining.length > 1 ? remaining.shift()! : (remaining[0] ?? { status: 'running' })
    return jsonResponse(200, status)
  })

  return { fetchMock, queue, calls }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

// Interval NHỎ (không phải 0, xem tránh CPU-bound loop vô hạn nếu logic có lỗi) — bài
// kiểm không khẳng định gì về THỜI GIAN tuyệt đối (quy tắc bắt buộc spec), chỉ dùng số
// này để vòng poll tiến nhanh trong lúc chạy test.
const FAST_POLL_MS = 1

describe('useRunQuery — bốn tình huống bất đồng bộ bắt buộc của Phần A', () => {
  it('unmount giữa chừng: không setState sau khi rời cây, không ném lỗi React', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)
    // `console.error` là nơi React log cảnh báo "state update ... not wrapped in
    // act(...)" khi một hook cập nhật state SAU khi component đã unmount — spy nó để
    // bài kiểm có một khẳng định THẬT, không chỉ "không ném lỗi" (một hook rò rỉ vẫn
    // chạy xong không throw, chỉ log cảnh báo).
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { result, unmount } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select 1'))
    await waitFor(() => expect(result.current.state.phase).toBe('running'))

    unmount()
    // Để vòng poll còn dang dở có cơ hội "tỉnh dậy" sau unmount — nếu hook không tự
    // dừng, đây là lúc React sẽ cảnh báo "setState trên component đã unmount".
    await new Promise((r) => setTimeout(r, 20))

    expect(consoleError).not.toHaveBeenCalled()
    consoleError.mockRestore()
  })

  it('bấm chạy hai lần: chỉ MỘT vòng poll sống sót, không hỏi chồng cho lượt cũ', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)

    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select 1')) // -> q1
    await waitFor(() => expect(result.current.state.queryId).toBe('q1'))

    act(() => result.current.run('lh1', 'select 2')) // -> q2, phải THẮNG
    await waitFor(() => expect(result.current.state.queryId).toBe('q2'))

    backend.queue('q2', { status: 'succeeded', columns: [], rows: [] })
    await waitFor(() => expect(result.current.state.phase).toBe('succeeded'))
    expect(result.current.state.queryId).toBe('q2')

    // Để vòng poll q1 (nếu còn sống) có cơ hội ghi đè — nó không được phép.
    await new Promise((r) => setTimeout(r, 20))
    expect(result.current.state.queryId).toBe('q2')
    expect(result.current.state.phase).toBe('succeeded')
  })

  it('query xong TRƯỚC lần hỏi đầu tiên: vẫn hiện kết quả, không kẹt ở running', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)
    // Xếp SẴN 'succeeded' — lần GET đầu tiên (và duy nhất) của hook đã thấy nó xong.

    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select 1'))
    await waitFor(() => expect(result.current.state.queryId).toBe('q1'))
    backend.queue('q1', { status: 'succeeded', columns: [{ name: 'n', type: 'int64' }], rows: [[1]] })

    await waitFor(() => expect(result.current.state.phase).toBe('succeeded'))
    expect(result.current.state.result?.rows).toEqual([[1]])
  })

  it('huỷ rồi chạy lại: kết quả CŨ không đè lên kết quả MỚI', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)

    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select slow'))
    await waitFor(() => expect(result.current.state.queryId).toBe('q1'))

    act(() => result.current.cancel())

    act(() => result.current.run('lh1', 'select fast')) // -> q2
    await waitFor(() => expect(result.current.state.queryId).toBe('q2'))
    backend.queue('q2', { status: 'succeeded', columns: [], rows: [] })
    await waitFor(() => expect(result.current.state.phase).toBe('succeeded'))

    // Phản hồi CHẬM của DELETE q1 (hoặc một vòng poll trễ của q1) đến sau — không được
    // phép ghi 'cancelled' đè lên kết quả 'succeeded' của q2 vừa có.
    await new Promise((r) => setTimeout(r, 20))
    expect(result.current.state.queryId).toBe('q2')
    expect(result.current.state.phase).toBe('succeeded')
  })
})

describe('useRunQuery — huỷ gọi ĐÚNG DELETE, không chỉ đổi giao diện', () => {
  it('cancel() gọi DELETE /api/v1/query/{id} — quan sát LỜI GỌI MẠNG, không chỉ state', async () => {
    // Chứng minh đỏ 1 của Phần A: một `cancel` chỉ đổi `state.phase` mà không gọi
    // mạng phải làm bài này ĐỎ.
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)

    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select 1'))
    await waitFor(() => expect(result.current.state.phase).toBe('running'))

    act(() => result.current.cancel())
    await waitFor(() => expect(result.current.state.phase).toBe('cancelled'))

    expect(
      backend.calls.some((c) => c.method === 'DELETE' && c.url === '/api/v1/query/q1'),
    ).toBe(true)
  })

  it('cancel() khi KHÔNG có gì đang chạy không gọi mạng', () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)
    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.cancel())
    expect(backend.calls.length).toBe(0)
  })
})

describe('useRunQuery — lỗi nộp query (400/403) tách khỏi lỗi thực thi', () => {
  it('lỗi cú pháp lúc POST đi vào submitError, KHÔNG vào error (chỗ dành cho lỗi thực thi)', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      jsonResponse(400, {
        detail: { message: 'bad', errors: [{ line: 1, column: 1, message: 'bad' }] },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'not sql'))
    await waitFor(() => expect(result.current.state.submitError).not.toBeNull())
    expect(result.current.state.error).toBeNull()
    expect(result.current.state.phase).toBe('idle')
  })
})

describe('useRunQuery — vượt giới hạn khác lỗi thực thi thường', () => {
  it('gắn cờ overLimit khi thông điệp khớp "byte cap"', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)
    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select huge'))
    await waitFor(() => expect(result.current.state.queryId).toBe('q1'))
    backend.queue('q1', {
      status: 'failed',
      error: 'query would scan 999 bytes, over the 100 byte cap — rejected before reading any data',
    })
    await waitFor(() => expect(result.current.state.phase).toBe('failed'))
    expect(result.current.state.overLimit).toBe(true)
  })

  it('KHÔNG gắn cờ overLimit cho một lỗi thực thi bình thường', async () => {
    const backend = fakeBackend()
    vi.stubGlobal('fetch', backend.fetchMock)
    const { result } = renderHook(() => useRunQuery(FAST_POLL_MS))
    act(() => result.current.run('lh1', 'select foo'))
    await waitFor(() => expect(result.current.state.queryId).toBe('q1'))
    backend.queue('q1', { status: 'failed', error: 'Binder Error: column "foo" not found' })
    await waitFor(() => expect(result.current.state.phase).toBe('failed'))
    expect(result.current.state.overLimit).toBe(false)
  })
})
