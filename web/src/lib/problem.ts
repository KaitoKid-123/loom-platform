/**
 * Đọc `application/problem+json` (RFC 9457) thành lỗi có kiểu.
 *
 * Backend trả `errors[]` kèm `loc` cho lỗi validate từng trường. Bỏ nó đi là bắt
 * người dùng tự đoán ô nào sai trong một form mười ô — và backend đã trả giá để có
 * nó (xem `errors.py`, Task 1 và Task 22).
 */
export interface ProblemBody {
  type?: string
  title: string
  status: number
  detail?: string
  instance?: string
  errors?: Array<{ loc: (string | number)[]; msg: string; type: string }>
}

export class ProblemError extends Error {
  readonly status: number
  readonly body: ProblemBody | null
  /** Tên trường → thông báo, lấy phần CUỐI của `loc` (`["body","name"]` → `name`). */
  readonly fieldErrors: Record<string, string>

  constructor(status: number, body: ProblemBody | null, fallback: string) {
    // `detail` trước `title`: `detail` là câu của server nói CHUYỆN GÌ vừa xảy ra
    // ("bản hiện tại 9"), còn `title` chỉ là tên mã trạng thái.
    super(body?.detail || body?.title || fallback)
    this.name = 'ProblemError'
    this.status = status
    this.body = body
    this.fieldErrors = Object.fromEntries(
      (body?.errors ?? []).map((e) => [String(e.loc[e.loc.length - 1]), e.msg]),
    )
  }
}

export async function readProblem(response: Response): Promise<ProblemBody | null> {
  // CHỈ tin `detail` khi server TỰ KHAI là problem+json.
  //
  // Hai phép phòng vệ dưới đây làm hai việc KHÁC nhau, và đã kiểm bằng cách gỡ từng
  // cái ra:
  //
  // - Kiểm content-type chặn việc trình bày JSON của một MÁY TRUNG GIAN như thông báo
  //   của API. Một gateway trả `{"detail":"invalid upstream token"}` kèm
  //   `application/json` parse trót lọt, và `detail` của nó sẽ được hiện cho người
  //   dùng như thể Loom vừa giải thích — gửi người đọc đi tìm lỗi xác thực trong Loom
  //   trong khi Loom đang chết. Chỉ `try/catch` thôi KHÔNG bắt được ca này.
  // - `try/catch` lo phần body không phải JSON, ví dụ nginx trả 502 dạng HTML.
  if (!response.headers.get('content-type')?.includes('application/problem+json')) {
    return null
  }
  try {
    return (await response.json()) as ProblemBody
  } catch {
    return null
  }
}
