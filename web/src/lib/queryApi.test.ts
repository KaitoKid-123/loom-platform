import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  QueryPermissionError,
  QuerySubmitError,
  QuerySyntaxError,
  cancelQuery,
  fetchQueryStatus,
  isOverLimitError,
  submitQuery,
} from './queryApi'

function jsonResponse(status: number, body: unknown, contentType = 'application/json') {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': contentType } })
}

function stub(factory: (input: RequestInfo | URL, init?: RequestInit) => Response) {
  const mock = vi.fn<typeof fetch>(async (input, init) => factory(input, init))
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('submitQuery', () => {
  it('trả query_id khi 202', async () => {
    stub(() => jsonResponse(202, { query_id: 'q1' }))
    const result = await submitQuery('lh1', 'select 1')
    expect(result.queryId).toBe('q1')
  })

  it('gửi đúng lakehouse_id và sql trong thân request', async () => {
    const mock = stub(() => jsonResponse(202, { query_id: 'q1' }))
    await submitQuery('lh1', 'select 1')
    const init = mock.mock.calls[0][1]
    expect(JSON.parse(String(init?.body))).toEqual({ lakehouse_id: 'lh1', sql: 'select 1' })
  })

  it('400 với errors[] ném QuerySyntaxError kèm dòng/cột — content-type JSON trần, KHÔNG problem+json', async () => {
    // `loom-api` chuyển tiếp thân phản hồi của `loom-query` NGUYÊN VẸN (`_forward`,
    // xem `routers/query.py`), không đi qua `install_error_handlers` — content-type
    // vẫn là `application/json`, không phải `application/problem+json`.
    stub(() =>
      jsonResponse(400, {
        detail: {
          message: 'the SQL failed to parse',
          errors: [{ line: 3, column: 10, message: "expected FROM, got 'form'" }],
        },
      }),
    )
    await expect(submitQuery('lh1', 'bad sql')).rejects.toBeInstanceOf(QuerySyntaxError)
    try {
      await submitQuery('lh1', 'bad sql')
      expect.unreachable('phải ném QuerySyntaxError')
    } catch (err) {
      expect(err).toBeInstanceOf(QuerySyntaxError)
      expect((err as QuerySyntaxError).issues).toEqual([
        { line: 3, column: 10, message: "expected FROM, got 'form'" },
      ])
    }
  })

  it('403 ném QueryPermissionError, KHÔNG lẫn với lỗi cú pháp', async () => {
    stub(() => jsonResponse(403, { detail: 'you do not have permission to run this query' }))
    await expect(submitQuery('lh1', 'select 1')).rejects.toBeInstanceOf(QueryPermissionError)
  })

  it('400 KHÔNG kèm errors[] (vd. tên bảng thiếu namespace) ném QuerySubmitError với thông điệp server', async () => {
    stub(() => jsonResponse(400, { detail: "table 'orders' has no namespace" }))
    try {
      await submitQuery('lh1', 'select * from orders')
      expect.unreachable('phải ném QuerySubmitError')
    } catch (err) {
      expect(err).toBeInstanceOf(QuerySubmitError)
      expect((err as Error).message).toBe("table 'orders' has no namespace")
    }
  })

  it('404 (lakehouse không tồn tại, application/problem+json từ CHÍNH loom-api) vẫn đọc được detail', async () => {
    stub(() =>
      jsonResponse(
        404,
        { title: 'Not Found', status: 404, detail: 'no lakehouse with this id' },
        'application/problem+json',
      ),
    )
    try {
      await submitQuery('lh-missing', 'select 1')
      expect.unreachable('phải ném QuerySubmitError')
    } catch (err) {
      expect(err).toBeInstanceOf(QuerySubmitError)
      expect((err as Error).message).toBe('no lakehouse with this id')
    }
  })
})

describe('fetchQueryStatus', () => {
  it('trả nguyên vẹn thân phản hồi', async () => {
    stub(() => jsonResponse(200, { status: 'succeeded', columns: [], rows: [] }))
    const status = await fetchQueryStatus('q1')
    expect(status.status).toBe('succeeded')
  })
})

describe('cancelQuery', () => {
  it('gọi DELETE đúng đường dẫn', async () => {
    const mock = stub(() => new Response(null, { status: 202 }))
    await cancelQuery('q1')
    expect(mock).toHaveBeenCalledWith(
      '/api/v1/query/q1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})

describe('isOverLimitError', () => {
  it('khớp thông điệp byte cap của ScanBytesExceeded', () => {
    expect(
      isOverLimitError('query would scan 999 bytes, over the 100 byte cap — rejected before reading any data'),
    ).toBe(true)
  })

  it('khớp thông điệp time limit của TimeoutError', () => {
    expect(isOverLimitError('query exceeded the 120s time limit and was stopped')).toBe(true)
  })

  it('KHÔNG khớp một lỗi runtime bình thường', () => {
    expect(isOverLimitError('Binder Error: column "foo" not found')).toBe(false)
  })
})
