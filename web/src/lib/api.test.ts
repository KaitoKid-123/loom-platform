import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ConflictError,
  NotFoundError,
  PreconditionRequiredError,
  UnauthorizedError,
  apiDelete,
  apiGetWithEtag,
  apiPatch,
  apiPostJson,
} from './api'
import { ProblemError } from './problem'
function problem(status: number, body: Record<string, unknown>) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/problem+json' },
  })
}

// Kiểu `typeof fetch` là bắt buộc, không phải cho đẹp: `vi.fn(async () => ...)` suy
// ra một hàm KHÔNG tham số, nên `mock.calls[0]` là tuple rỗng và mọi phép đọc
// `calls[0][1]` — tức phần `init` chứa headers — không biên dịch được.
function stub(factory: () => Response) {
  const mock = vi.fn<typeof fetch>(async () => factory())
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api', () => {
  it('đọc ETag từ header cùng với body', async () => {
    stub(
      () =>
        new Response(JSON.stringify({ id: 'x' }), {
          status: 200,
          headers: { etag: 'W/"7"' },
        }),
    )
    const { data, etag } = await apiGetWithEtag<{ id: string }>('/api/v1/items/x')
    expect(data.id).toBe('x')
    expect(etag).toBe('W/"7"')
  })

  it('gửi If-Match khi PATCH', async () => {
    const fetchMock = stub(
      () => new Response(JSON.stringify({}), { status: 200, headers: { etag: 'W/"8"' } }),
    )
    await apiPatch('/api/v1/items/x', { display_name: 'A' }, 'W/"7"')
    const init = fetchMock.mock.calls[0][1]
    expect((init?.headers as Record<string, string>)['If-Match']).toBe('W/"7"')
  })

  it('KHÔNG gửi If-Match rỗng khi chưa có etag', async () => {
    // Gửi chuỗi rỗng làm server trả 400 ("If-Match không đúng định dạng") thay vì
    // 428, và 428 là mã duy nhất nói cho client biết phải thêm header nào.
    const fetchMock = stub(() => new Response(JSON.stringify({}), { status: 200 }))
    await apiPatch('/api/v1/items/x', {}, undefined)
    const init = fetchMock.mock.calls[0][1]
    expect(init?.headers as Record<string, string>).not.toHaveProperty('If-Match')
  })

  it('412 thành ConflictError mang thông báo của server', async () => {
    stub(() =>
      problem(412, {
        title: 'Precondition Failed',
        status: 412,
        detail: 'somebody else changed this item (current version is 9)',
      }),
    )
    await expect(apiPatch('/api/v1/items/x', {}, 'W/"7"')).rejects.toThrow(ConflictError)
    // Thông báo của server PHẢI đi tới người dùng: nó nói bản hiện tại là mấy. Thay
    // bằng "Có lỗi" là bỏ đi thông tin duy nhất giúp họ hiểu chuyện gì vừa xảy ra.
    await expect(apiPatch('/api/v1/items/x', {}, 'W/"7"')).rejects.toThrow(/current version is 9/)
  })

  it('428 thành PreconditionRequiredError riêng, không lẫn với 412', async () => {
    // Hai mã nói hai chuyện khác nhau — "bản của bạn cũ" so với "bạn quên header".
    // Gộp chúng là bỏ đúng cái phân biệt mà backend đã cố ý tạo ra.
    stub(() => problem(428, { title: 'Precondition Required', status: 428 }))
    const error = await apiPatch('/api/v1/items/x', {}, undefined).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(PreconditionRequiredError)
    expect(error).not.toBeInstanceOf(ConflictError)
  })

  it('404 thành NotFoundError riêng', async () => {
    stub(() => problem(404, { title: 'Not Found', status: 404 }))
    await expect(apiGetWithEtag('/api/v1/items/x')).rejects.toThrow(NotFoundError)
  })

  it('401 thành UnauthorizedError, không phải ProblemError', async () => {
    // `App.tsx` phân biệt riêng 401 để chuyển sang đăng nhập. Trộn nó vào
    // ProblemError là biến "hết phiên" thành một thông báo lỗi người dùng bó tay.
    stub(() => problem(401, { title: 'Unauthorized', status: 401 }))
    const error = await apiDelete('/api/v1/items/x').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(UnauthorizedError)
    expect(error).not.toBeInstanceOf(ProblemError)
  })

  it('lỗi 422 giữ được errors[] để gắn vào từng input', async () => {
    stub(() =>
      problem(422, {
        title: 'Unprocessable Content',
        status: 422,
        detail: 'the submitted data is not valid',
        errors: [
          { loc: ['body', 'name'], msg: 'invalid format', type: 'string_pattern_mismatch' },
        ],
      }),
    )
    const error = await apiPostJson('/api/v1/x', {}).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ProblemError)
    expect((error as ProblemError).fieldErrors.name).toBe('invalid format')
  })

  it('phản hồi không phải problem+json vẫn thành lỗi có nghĩa', async () => {
    // nginx trả 502 dạng HTML khi api chết. Không được để nó thành "undefined".
    stub(
      () =>
        new Response('<html>502 Bad Gateway</html>', {
          status: 502,
          headers: { 'content-type': 'text/html' },
        }),
    )
    await expect(apiDelete('/api/v1/items/x')).rejects.toThrow(/502/)
  })

  it('KHÔNG trình bày JSON của một máy trung gian như thông báo của API', async () => {
    // Đây là việc mà phép kiểm content-type thật sự làm, và `try/catch` quanh
    // `response.json()` KHÔNG làm được: một gateway trả JSON hợp lệ kèm
    // `application/json` sẽ parse trót lọt, và `detail` của nó được hiện cho người
    // dùng như thể API của Loom vừa giải thích chuyện gì xảy ra. "invalid upstream
    // token" gửi người đọc đi tìm lỗi xác thực trong Loom trong khi Loom đang chết.
    stub(
      () =>
        new Response(JSON.stringify({ detail: 'invalid upstream token' }), {
          status: 502,
          headers: { 'content-type': 'application/json' },
        }),
    )
    const error = await apiDelete('/api/v1/items/x').catch((e: unknown) => e)
    expect((error as Error).message).toContain('502')
    expect((error as Error).message).not.toContain('invalid upstream token')
  })

  it('apiDelete không kèm body thì không đặt Content-Type', async () => {
    // Một DELETE mang Content-Type mà không có body làm vài proxy và framework
    // chờ đọc body rồi mới chuyển tiếp.
    const fetchMock = stub(() => new Response(null, { status: 204 }))
    await apiDelete('/api/v1/items/x')
    const init = fetchMock.mock.calls[0][1]
    expect(init?.headers as Record<string, string>).not.toHaveProperty('Content-Type')
    expect(init?.body).toBeUndefined()
  })

  it('mọi lời gọi đều kèm cookie', async () => {
    // Kiến trúc BFF: phiên nằm trong cookie httpOnly. Thiếu `credentials` thì mọi
    // lời gọi thành 401 và trông y như hết phiên.
    const fetchMock = stub(() => new Response(JSON.stringify({}), { status: 200 }))
    await apiGetWithEtag('/api/v1/x')
    await apiPatch('/api/v1/x', {}, 'W/"1"')
    await apiPostJson('/api/v1/x', {})
    for (const call of fetchMock.mock.calls) {
      expect(call[1]?.credentials).toBe('include')
    }
  })
})
