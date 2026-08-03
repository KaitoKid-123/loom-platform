export class UnauthorizedError extends Error {}
export class ApiError extends Error {}

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
