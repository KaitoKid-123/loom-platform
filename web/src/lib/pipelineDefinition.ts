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

/**
 * Một hàng `pipeline_step_run` như `PipelineRunDetail.steps` trả về (xem
 * `PipelineStepRunOut` ở `packages/core/src/loom_core/schemas.py`).
 *
 * `status` và `step_type` là `string` chứ không phải union đóng — khớp lựa chọn của
 * server (`str`, không `Literal`) trên CHÍNH hai trường này: đây là phản hồi dựng từ
 * DỮ LIỆU ĐÃ LƯU, và một union đóng ở phía đọc chỉ đổi chỗ hỏng (từ "hàng lạ hiển thị
 * được" thành "hàng lạ vỡ kiểu"), không đổi được sự thật là có thể có hàng lạ.
 *
 * `ingest_run_id`, `query_id`, `started_at`, `finished_at` đánh dấu `?:` dù server LUÔN
 * gửi bốn khoá này (kể cả khi giá trị là `null` — không có `exclude_unset`/`exclude_none`
 * nào trên đường `GET /pipeline-runs/{run_id}`, xem `_step_out` ở
 * `services/api/src/loom_api/routers/pipeline_runs.py`). Đánh dấu optional ở đây vẫn
 * ĐÚNG cho module này: `mergeStepStatus`/`diagramNodes` không đọc bốn trường đó, nên
 * bắt mọi nơi dựng `StepRunView` — kể cả test — phải điền bốn trường không dùng tới là
 * phí, không phải an toàn hơn. Một module SAU đọc tới chúng (vd. mở link
 * `GET /ingest/{run_id}` từ `ingest_run_id`) nên viết lại kiểu chặt hơn ở NGAY module
 * đó thay vì siết ở đây cho một thứ nó chưa cần.
 */
export interface StepRunView {
  step_index: number
  step_type: string
  status: string
  error: string | null
  ingest_run_id?: string | null
  query_id?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface MergedStep {
  index: number
  step: PipelineStepView
  /** `null` khi run này không có hàng nào cho `index` — không phải khi nó chưa chạy. */
  run: StepRunView | null
  /** `'none'` nghĩa là KHÔNG BIẾT, khác hẳn `'pending'` (biết là chưa tới lượt). */
  status: string
}

export interface DiagramNode {
  index: number
  type: 'ingest' | 'sql'
  label: string
  status: string
}

/**
 * Ghép chuỗi bước hiện tại với các hàng step-run — **theo `step_index`, KHÔNG theo vị
 * trí mảng**.
 *
 * Vì sao điều này quan trọng: một run là một sự kiện đã xảy ra trên định nghĩa lúc ĐÓ,
 * còn `steps` là định nghĩa BÂY GIỜ. Người dùng sửa pipeline sau khi run chạy là chuyện
 * bình thường, nên hai mảng có thể khác độ dài và `step_index` có thể không liên tục.
 * Ghép theo vị trí trong trường hợp đó gán trạng thái của một bước cho một bước KHÁC —
 * không lỗi, không cảnh báo, chỉ nói sai bước nào đã hỏng. Đó là kiểu sai tệ nhất ở
 * đúng màn hình mà người ta mở ra để tìm hiểu vì sao run hỏng.
 *
 * Một `step_index` trỏ ra ngoài chuỗi hiện tại bị BỎ QUA thay vì đẻ thêm node: sơ đồ vẽ
 * định nghĩa hiện tại, và một node không có bước tương ứng thì không sửa được, không
 * xoá được, không có nghĩa gì.
 */
export function mergeStepStatus(
  steps: PipelineStepView[],
  stepRuns: StepRunView[],
): MergedStep[] {
  const byIndex = new Map<number, StepRunView>()
  for (const run of stepRuns) {
    if (typeof run.step_index === 'number') byIndex.set(run.step_index, run)
  }
  return steps.map((step, index) => {
    const run = byIndex.get(index) ?? null
    return { index, step, run, status: run?.status ?? 'none' }
  })
}

/**
 * Node của sơ đồ — DẪN XUẤT từ `steps`, không bao giờ lưu.
 *
 * `stepRuns` không truyền thì mọi node mang `status: 'none'`. Đó là cách trang pipeline
 * dùng nó (vẽ HÌNH DẠNG), còn chi tiết run truyền vào để vẽ hình dạng CỘNG trạng thái.
 * Một hàm cho hai chỗ là lý do phương án C của spec rẻ hơn nó trông.
 *
 * `label` của bước nạp lấy nguyên `stream` (vd. `public.orders`), KHÔNG cắt bớt: cắt ở
 * đây là cắt trước khi biết node rộng bao nhiêu pixel, tức đoán mù. Nơi biết bề rộng
 * thật là `PipelineDiagram` — cắt bằng CSS (`text-overflow: ellipsis`) ở đó, kèm
 * `title` mang tên đầy đủ, là việc của module đó, không phải của module thuần này.
 */
export function diagramNodes(
  steps: PipelineStepView[],
  stepRuns: StepRunView[] = [],
): DiagramNode[] {
  return mergeStepStatus(steps, stepRuns).map(({ index, step, status }) => ({
    index,
    type: step.type,
    label: step.type === 'ingest' ? (step.ingest?.stream ?? 'Ingest') : 'SQL',
    status,
  }))
}
