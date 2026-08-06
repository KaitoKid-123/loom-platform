import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConflictError } from './api'
import type { TreeItem } from './folderTree'
import { ProblemError } from './problem'
import type { ItemPage } from './useItems'
import { describeError, useRenameItem } from './useItemMutations'

const ITEM: TreeItem = {
  id: 'i1',
  name: 'a',
  display_name: 'Cũ',
  folder_path: '/',
  type: 'sql_script',
  version: 1,
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function seeded(keys: readonly (string | null)[][] = [['items', 'ws1', null]]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  for (const key of keys) {
    qc.setQueryData<ItemPage>(key, { items: [ITEM], next_cursor: null })
  }
  return qc
}

function nameIn(qc: QueryClient, key: readonly (string | null)[] = ['items', 'ws1', null]) {
  return qc.getQueryData<ItemPage>(key)?.items[0].display_name
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useRenameItem', () => {
  it('đổi tên hiện ngay trong cache trước khi server trả lời', async () => {
    const qc = seeded()
    let release: (v: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        () =>
          new Promise<Response>((r) => {
            release = r
          }),
      ),
    )

    const { result } = renderHook(() => useRenameItem('ws1'), { wrapper: wrapper(qc) })
    act(() => {
      result.current.mutate({ itemId: 'i1', etag: 'W/"1"', displayName: 'Mới' })
    })

    // Cập nhật lạc quan: cache đổi NGAY, chưa cần server trả lời.
    await waitFor(() => expect(nameIn(qc)).toBe('Mới'))

    release(
      new Response(JSON.stringify({ ...ITEM, display_name: 'Mới', version: 2 }), {
        status: 200,
        headers: { etag: 'W/"2"' },
      }),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
  })

  it('ghi lạc quan vào MỌI bộ lọc, không riêng bộ lọc đang xem', async () => {
    // `useItems` đưa `type` vào queryKey, nên cache có nhiều mục cho cùng workspace.
    // Ghi vào đúng một mục thì đổi bộ lọc là thấy tên cũ quay lại.
    const qc = seeded([
      ['items', 'ws1', null],
      ['items', 'ws1', 'sql_script'],
    ])
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise<Response>(() => {})),
    )

    const { result } = renderHook(() => useRenameItem('ws1'), { wrapper: wrapper(qc) })
    act(() => {
      result.current.mutate({ itemId: 'i1', etag: 'W/"1"', displayName: 'Mới' })
    })

    await waitFor(() => expect(nameIn(qc)).toBe('Mới'))
    expect(nameIn(qc, ['items', 'ws1', 'sql_script'])).toBe('Mới')
  })

  it('412 trả cache về giá trị cũ VÀ báo có người khác vừa đổi', async () => {
    const qc = seeded()
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({
              title: 'Precondition Failed',
              status: 412,
              detail: 'somebody else changed this item (current version is 5)',
            }),
            { status: 412, headers: { 'content-type': 'application/problem+json' } },
          ),
      ),
    )

    const { result } = renderHook(() => useRenameItem('ws1'), { wrapper: wrapper(qc) })
    act(() => {
      result.current.mutate({ itemId: 'i1', etag: 'W/"1"', displayName: 'Mới' })
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    // Rollback: tên cũ trở lại.
    expect(nameIn(qc)).toBe('Cũ')
    // Và thông báo phải nói LÝ DO, không chỉ nhấp nháy về giá trị cũ. Không có nó,
    // người dùng thấy tên mình vừa gõ biến mất và tưởng ứng dụng hỏng.
    expect(result.current.error?.message).toMatch(/somebody else/)
    expect(result.current.error?.message).toMatch(/current version is 5/)
  })

  it('lỗi mạng cũng rollback, không để cache lệch với server', async () => {
    const qc = seeded()
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(async () => {
        throw new TypeError('mạng hỏng')
      }),
    )

    const { result } = renderHook(() => useRenameItem('ws1'), { wrapper: wrapper(qc) })
    act(() => {
      result.current.mutate({ itemId: 'i1', etag: 'W/"1"', displayName: 'Mới' })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(nameIn(qc)).toBe('Cũ')
  })

  it('refetch đang bay KHÔNG được ghi đè giá trị lạc quan', async () => {
    // Lỗi khó thấy nhất của task: người dùng thấy tên mình vừa sửa hiện ra rồi tự
    // hoàn tác, không lỗi, không thông báo.
    const qc = seeded()

    let releaseRefetch: (v: ItemPage) => void = () => {}
    const refetch = qc.fetchQuery<ItemPage>({
      queryKey: ['items', 'ws1', null],
      queryFn: () =>
        new Promise<ItemPage>((r) => {
          releaseRefetch = r
        }),
    })
    // Refetch đã bay; mutation dùng fetch riêng và không bao giờ trả lời.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => new Promise<Response>(() => {})),
    )

    const { result } = renderHook(() => useRenameItem('ws1'), { wrapper: wrapper(qc) })
    act(() => {
      result.current.mutate({ itemId: 'i1', etag: 'W/"1"', displayName: 'Mới' })
    })
    await waitFor(() => expect(nameIn(qc)).toBe('Mới'))

    // Giờ refetch cũ về, mang dữ liệu CŨ.
    releaseRefetch({ items: [ITEM], next_cursor: null })
    await refetch.catch(() => undefined)
    await new Promise((r) => setTimeout(r, 20))

    expect(nameIn(qc)).toBe('Mới')
  })
})

describe('describeError', () => {
  it('412 giữ thông báo server VÀ thêm bước tiếp theo', () => {
    const error = new ConflictError(
      412,
      { title: 'Precondition Failed', status: 412, detail: 'current version is 5' },
      'dự phòng',
    )
    const text = describeError(error)
    expect(text).toContain('current version is 5')
    expect(text).toMatch(/tải lại/i)
  })

  it('422 gộp lỗi từng trường thành câu đọc được', () => {
    const error = new ProblemError(
      422,
      {
        title: 'Unprocessable Content',
        status: 422,
        errors: [{ loc: ['body', 'name'], msg: 'invalid format', type: 'x' }],
      },
      'dự phòng',
    )
    expect(describeError(error)).toBe('name: invalid format')
  })

  it('lỗi thường đi qua nguyên văn', () => {
    expect(describeError(new Error('mạng hỏng'))).toBe('mạng hỏng')
  })
})
