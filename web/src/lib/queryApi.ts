import { UnauthorizedError } from './api'

/**
 * Client cho `POST/GET/DELETE /api/v1/query` — KHÔNG dùng `raise()` của `api.ts`.
 *
 * Lý do tách riêng: `services/api/src/loom_api/routers/query.py` (`_forward`) chuyển
 * tiếp status code VÀ THÂN PHẢN HỒI NGUYÊN VẸN từ `loom-query`, KHÔNG đi qua
 * `install_error_handlers`/`ProblemDetail` của `loom-api` (đọc docstring module đó).
 * Nghĩa là lỗi 400/403 của route này có content-type `application/json` TRẦN — không
 * phải `application/problem+json` — với thân `{"detail": "..."}` (chuỗi) HOẶC
 * `{"detail": {"message": "...", "errors": [{"line", "column", "message"}]}}` (lỗi cú
 * pháp — xem `loom_query.authz.SqlSyntaxError`). `readProblem` của `api.ts` đọc
 * content-type trước khi tin `detail`, nên áp nó vào đây sẽ luôn trả `null` và bỏ mất
 * chính xác thứ Phần A cần: dòng/cột lỗi cú pháp.
 *
 * (404 "no lakehouse with this id" từ CHÍNH `loom-api`, khi `lakehouse_id` không tồn
 * tại, LÀ `application/problem+json` — nhưng `detail` của nó cũng chỉ là một chuỗi, và
 * `readQueryErrorDetail` bên dưới đọc thẳng JSON thay vì phân biệt content-type, nên
 * hình dạng đó vẫn parse đúng.)
 */

export interface SqlSyntaxIssue {
  line: number
  column: number
  message: string
}

/** 400 kèm dòng/cột — `loom_sql.validate` qua `authz.SqlSyntaxError`. */
export class QuerySyntaxError extends Error {
  readonly issues: SqlSyntaxIssue[]
  constructor(issues: SqlSyntaxIssue[]) {
    super(
      issues.length > 0
        ? issues.map((i) => `line ${i.line}, column ${i.column}: ${i.message}`).join('; ')
        : 'the SQL failed to parse',
    )
    this.name = 'QuerySyntaxError'
    this.issues = issues
  }
}

/** 403 — `authz.QueryForbidden`. Tách khỏi lỗi cú pháp: người dùng cần biết đây là
 * chuyện QUYỀN, không phải chuyện họ gõ sai. */
export class QueryPermissionError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'QueryPermissionError'
  }
}

/** Mọi lỗi 400 KHÁC lỗi cú pháp (tên bảng thiếu namespace, nguồn ngoài catalog,
 * path Files/ không an toàn...), cùng lỗi mạng/máy chủ chung. */
export class QuerySubmitError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'QuerySubmitError'
  }
}

interface QueryErrorDetail {
  message?: string
  errors?: SqlSyntaxIssue[]
}

function isSyntaxIssue(value: unknown): value is SqlSyntaxIssue {
  return (
    !!value &&
    typeof value === 'object' &&
    typeof (value as SqlSyntaxIssue).line === 'number' &&
    typeof (value as SqlSyntaxIssue).column === 'number' &&
    typeof (value as SqlSyntaxIssue).message === 'string'
  )
}

/** Đọc `detail` bất kể nó là chuỗi (mọi lỗi 40x khác) hay object `{message, errors}`
 * (lỗi cú pháp) — xem docstring module cho lý do không lọc theo content-type ở đây. */
async function readQueryErrorDetail(response: Response): Promise<QueryErrorDetail | null> {
  try {
    const body: unknown = await response.json()
    if (!body || typeof body !== 'object' || !('detail' in body)) return null
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return { message: detail }
    if (detail && typeof detail === 'object') {
      const raw = detail as { message?: unknown; errors?: unknown }
      return {
        message: typeof raw.message === 'string' ? raw.message : undefined,
        errors: Array.isArray(raw.errors) ? raw.errors.filter(isSyntaxIssue) : undefined,
      }
    }
    return null
  } catch {
    return null
  }
}

export interface SubmittedQuery {
  queryId: string
}

/** `POST /api/v1/query`. Ném MỘT trong ba loại lỗi ở trên — `SqlEditorPanel` phân biệt
 * chúng bằng `instanceof`, không bằng đọc lại `message`. */
export async function submitQuery(lakehouseId: string, sql: string): Promise<SubmittedQuery> {
  const response = await fetch('/api/v1/query', {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ lakehouse_id: lakehouseId, sql }),
  })
  if (response.status === 401) throw new UnauthorizedError('not signed in')
  if (!response.ok) {
    const detail = await readQueryErrorDetail(response)
    if (response.status === 400 && detail?.errors && detail.errors.length > 0) {
      throw new QuerySyntaxError(detail.errors)
    }
    if (response.status === 403) {
      throw new QueryPermissionError(
        detail?.message ?? 'you do not have permission to run this query',
      )
    }
    throw new QuerySubmitError(detail?.message ?? `POST /api/v1/query returned ${response.status}`)
  }
  const data = (await response.json()) as { query_id: string }
  return { queryId: data.query_id }
}

export interface QueryColumn {
  name: string
  type: string
}

/** Khớp `QueryStatusOut` (`services/loom-query/src/loom_query/schemas.py`), sau khi đi
 * qua `response_model_exclude_none=True` — mọi trường tuỳ chọn có thể vắng mặt hẳn. */
export interface QueryStatusResponse {
  status: 'running' | 'succeeded' | 'failed' | 'cancelled'
  columns?: QueryColumn[]
  rows?: unknown[][]
  error?: string
  truncated?: boolean
  row_count?: number
}

/** `GET /api/v1/query/{id}` — một lần hỏi, không tự lặp lại. Vòng lặp poll là việc của
 * `useRunQuery`, để hàm này giữ được dễ kiểm (một lời gọi mạng, một kết quả). */
export async function fetchQueryStatus(queryId: string): Promise<QueryStatusResponse> {
  const response = await fetch(`/api/v1/query/${queryId}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (response.status === 401) throw new UnauthorizedError('not signed in')
  if (!response.ok) {
    throw new QuerySubmitError(`GET /api/v1/query/${queryId} returned ${response.status}`)
  }
  return (await response.json()) as QueryStatusResponse
}

/** `DELETE /api/v1/query/{id}` — huỷ THẬT (xem `store.py`: `interrupt()` dừng hẳn
 * DuckDB, không chỉ đổi nhãn trạng thái). */
export async function cancelQuery(queryId: string): Promise<void> {
  const response = await fetch(`/api/v1/query/${queryId}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (response.status === 401) throw new UnauthorizedError('not signed in')
  if (!response.ok) {
    throw new QuerySubmitError(`DELETE /api/v1/query/${queryId} returned ${response.status}`)
  }
}

// Hai cụm CHÍNH XÁC do `runner.execute` sinh ra cho hai giới hạn cứng (đọc
// `services/loom-query/src/loom_query/runner.py`):
//   - ScanBytesExceeded: "...over the N byte cap — rejected before reading any data"
//   - TimeoutError:      "query exceeded the Ns time limit and was stopped"
// Một lỗi runtime BÌNH THƯỜNG của DuckDB (cột không tồn tại, sai kiểu, chia cho 0...)
// không chứa hai cụm này — khớp chuỗi là đủ để tách "quá lớn" khỏi "lỗi khác" mà không
// cần một mã lỗi cấu trúc riêng ở API (chưa có: mọi lỗi thực thi chỉ là một `error:
// string`, xem `runner.execute`/`store.set_failed`).
//
// NỢ ĐÃ BIẾT, và đừng tin ngược lại: KHÔNG có gì canh hợp đồng chuỗi này qua hai
// ngôn ngữ. `queryApi.test.ts` chép cứng cùng văn bản đó bằng TypeScript và không
// hề đọc Python, nên đổi tên `byte cap` bên `loom_query.limits` sẽ để `make test`,
// `make web-test` và `mypy` xanh nguyên trong khi giao diện lặng lẽ hiện "Query
// failed" thay cho "Query too large" mãi mãi. Cách chữa thật là một mã lỗi có cấu
// trúc trong phản hồi API — không phải một phép kiểm chuỗi khéo hơn.
const OVER_LIMIT_PATTERNS = [/byte cap/i, /time limit/i]

/** `status: "failed"` có phải vì query VƯỢT GIỚI HẠN (byte quét, thời gian) hay không —
 * khác hẳn một lỗi runtime do người dùng gõ sai (cột/bảng sai kiểu). Hai việc người
 * dùng phải làm khác nhau: viết lại câu hỏi so với thu hẹp phạm vi quét. */
export function isOverLimitError(message: string): boolean {
  return OVER_LIMIT_PATTERNS.some((pattern) => pattern.test(message))
}
