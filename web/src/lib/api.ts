import { ProblemError, readProblem } from './problem'

export class UnauthorizedError extends Error {}
export class ApiError extends Error {}

// Mỗi mã trạng thái mà API dùng có chủ đích đều được một lớp lỗi riêng. 412 và 428
// nói hai chuyện khác nhau — "bản của bạn cũ" so với "bạn quên header" — và gộp chúng
// là bỏ đúng cái phân biệt mà backend đã cố ý tạo ra (xem `routers/items.py`).
export class ConflictError extends ProblemError {}
export class PreconditionRequiredError extends ProblemError {}
export class NotFoundError extends ProblemError {}

const JSON_HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json' }

/** Luôn ném. Kiểu trả `never` để TypeScript biết dòng sau nó không chạy tới. */
async function raise(response: Response, path: string): Promise<never> {
  // 401 tách riêng và KHÔNG phải ProblemError: `App.tsx` phân biệt nó để chuyển sang
  // đăng nhập, còn mọi ProblemError khác thì hiện cho người dùng đọc. Trộn hai thứ
  // biến "hết phiên" thành một thông báo lỗi mà người dùng không làm gì được.
  if (response.status === 401) throw new UnauthorizedError('chưa đăng nhập')

  const body = await readProblem(response)
  const fallback = `${path} trả về ${response.status}`
  switch (response.status) {
    case 404:
      throw new NotFoundError(404, body, fallback)
    case 412:
      throw new ConflictError(412, body, fallback)
    case 428:
      throw new PreconditionRequiredError(428, body, fallback)
    default:
      throw new ProblemError(response.status, body, fallback)
  }
}

/** Mọi lời gọi API đi qua đây — luôn kèm cookie, luôn phân biệt 401 với lỗi thật. */
export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (response.status === 401) {
    throw new UnauthorizedError('chưa đăng nhập')
  }
  if (!response.ok) {
    throw new ApiError(`${path} trả về ${response.status}`)
  }
  return (await response.json()) as T
}

export async function apiPost(path: string): Promise<void> {
  const response = await fetch(path, { method: 'POST', credentials: 'include' })
  // `Response.ok` đã đúng với mọi 2xx kể cả 204, nên không cần kiểm riêng 204.
  if (!response.ok) {
    throw new ApiError(`${path} trả về ${response.status}`)
  }
}

export async function apiGetWithEtag<T>(
  path: string,
  signal?: AbortSignal,
): Promise<{ data: T; etag: string | null }> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!response.ok) await raise(response, path)
  return { data: (await response.json()) as T, etag: response.headers.get('etag') }
}

export async function apiPatch<T>(
  path: string,
  body: unknown,
  etag: string | undefined,
): Promise<{ data: T; etag: string | null }> {
  const headers: Record<string, string> = { ...JSON_HEADERS }
  // Gửi If-Match CHỈ khi có etag. Gửi chuỗi rỗng làm server trả 400 ("không đúng định
  // dạng") thay vì 428, và 428 là mã duy nhất nói cho client biết phải thêm header nào
  // để thử lại đúng cách.
  if (etag) headers['If-Match'] = etag
  const response = await fetch(path, {
    method: 'PATCH',
    credentials: 'include',
    headers,
    body: JSON.stringify(body),
  })
  if (!response.ok) await raise(response, path)
  return { data: (await response.json()) as T, etag: response.headers.get('etag') }
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!response.ok) await raise(response, path)
  return (await response.json()) as T
}

/** ETag cùng dữ liệu: `POST /items` mang ETag ngay trên phản hồi tạo (Task 22). */
export async function apiPostJsonWithEtag<T>(
  path: string,
  body: unknown,
): Promise<{ data: T; etag: string | null }> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!response.ok) await raise(response, path)
  return { data: (await response.json()) as T, etag: response.headers.get('etag') }
}

export async function apiDelete(path: string): Promise<void> {
  // Không body, và do đó KHÔNG Content-Type: một DELETE mang Content-Type mà không có
  // body làm vài proxy chờ đọc body trước khi chuyển tiếp. Endpoint thu quyền nhận
  // principal qua query string đúng vì lý do này (xem `routers/roles.py`).
  const response = await fetch(path, {
    method: 'DELETE',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) await raise(response, path)
}
