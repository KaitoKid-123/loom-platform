/**
 * Hình dạng `PipelineDefinition` ở phía giao diện — module THUẦN.
 *
 * Không react, không react-query, không fetch. Đây là chỗ DUY NHẤT biết `definition`
 * của một item `pipeline` trông thế nào, nên khi backend mở rẽ nhánh (không phải 3c)
 * thì chỉ file này và trình soạn phải đổi.
 *
 * Nguyên tắc bao trùm cả file: **không hàm nào ở đây được NÉM.** Chúng nhận `unknown`
 * lấy từ một phản hồi HTTP, và một `definition` lạ — sửa tay, một migration sau thêm
 * loại bước — không được phép hạ cả trang xuống trang lỗi của React Router. Đã gặp
 * thật ở Giai đoạn 0: `name.split` trên `undefined` làm trắng toàn bộ màn hình (xem
 * `initials()` ở `AppShell.tsx`). Bỏ qua thứ không đọc được và vẽ phần còn lại là câu
 * trả lời đúng; sập là câu trả lời sai cho cùng dữ liệu.
 *
 * Các trường khớp `IngestStepConfig`/`SqlStepConfig`/`PipelineStep` ở
 * `packages/core/src/loom_core/item_definitions.py`. Phía server gõ `lakehouse_id` và
 * `connection_id` là `uuid.UUID`; ở đây chúng chỉ là `string` — JSON qua dây không có
 * kiểu UUID, và việc kiểm chuỗi đó CÓ phải UUID hợp lệ hay không không phải việc của
 * module đọc/hiển thị này.
 */

export interface IngestStepConfig {
  lakehouse_id: string
  connection_id: string
  stream: string
  mode: 'full' | 'incremental'
}

export interface SqlStepConfig {
  lakehouse_id: string
  sql: string
}

/** Một bước trong chuỗi TUYẾN TÍNH. 3b không có rẽ nhánh — xem `PipelineStep` ở `item_definitions.py`. */
export interface PipelineStepView {
  type: 'ingest' | 'sql'
  ingest?: IngestStepConfig
  sql?: SqlStepConfig
}

const SQL_PREVIEW_LIMIT = 60

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function parseIngest(raw: unknown): IngestStepConfig | null {
  if (!isRecord(raw)) return null
  const lakehouse_id = asString(raw.lakehouse_id)
  const connection_id = asString(raw.connection_id)
  const stream = asString(raw.stream)
  const mode = raw.mode
  if (!lakehouse_id || !connection_id || !stream) return null
  if (mode !== 'full' && mode !== 'incremental') return null
  return { lakehouse_id, connection_id, stream, mode }
}

function parseSql(raw: unknown): SqlStepConfig | null {
  if (!isRecord(raw)) return null
  const lakehouse_id = asString(raw.lakehouse_id)
  // `sql` được phép RỖNG: người dùng thêm một bước rồi đi lấy câu SQL là trạng thái
  // bình thường của một bản nháp. Server mới là chỗ đòi `min_length=1` khi lưu.
  const sql = typeof raw.sql === 'string' ? raw.sql : null
  if (!lakehouse_id || sql === null) return null
  return { lakehouse_id, sql }
}

/** `definition.steps` đã lọc về những bước ĐỌC ĐƯỢC, giữ nguyên thứ tự. */
export function parseSteps(definition: unknown): PipelineStepView[] {
  if (!isRecord(definition)) return []
  const raw = definition.steps
  if (!Array.isArray(raw)) return []

  const steps: PipelineStepView[] = []
  for (const entry of raw) {
    if (!isRecord(entry)) continue
    if (entry.type === 'ingest') {
      const ingest = parseIngest(entry.ingest)
      if (ingest) steps.push({ type: 'ingest', ingest })
    } else if (entry.type === 'sql') {
      const sql = parseSql(entry.sql)
      if (sql) steps.push({ type: 'sql', sql })
    }
    // `type` lạ: bỏ qua. Một loại bước mà giao diện này chưa biết vẽ vẫn không được
    // làm mất những bước nó biết vẽ.
  }
  return steps
}

/** Một dòng mô tả cho danh sách bước. Luôn một dòng — bố cục hàng phụ thuộc điều đó. */
export function describeStep(step: PipelineStepView): string {
  if (step.type === 'ingest' && step.ingest) {
    return `Ingest · ${step.ingest.stream} · ${step.ingest.mode}`
  }
  if (step.type === 'sql' && step.sql) {
    // Gộp MỌI khoảng trắng, kể cả newline: một câu SQL nhiều dòng làm hàng danh sách
    // cao gấp năm và bố cục vỡ.
    const flat = step.sql.sql.replace(/\s+/g, ' ').trim()
    const preview =
      flat.length > SQL_PREVIEW_LIMIT ? `${flat.slice(0, SQL_PREVIEW_LIMIT)}…` : flat
    return `SQL · ${preview || '(chưa có câu lệnh)'}`
  }
  // Không ném: `parseSteps` đã loại các bước không khớp, nhưng hàm này xuất ra ngoài
  // nên nó phải tự trả lời được cho mọi đầu vào nó khai nhận.
  return step.type
}
