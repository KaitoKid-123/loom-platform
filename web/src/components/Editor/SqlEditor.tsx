import * as monaco from 'monaco-editor'
// LƯU Ý ĐO ĐƯỢC (bản thử ở `/tmp/…/monaco-experiment`, `npm run build` thật, không đoán):
// monaco-editor 0.56 thêm `exports` map trong package.json giới hạn deep-import.
// `monaco-editor/esm/vs/editor/editor.worker` (đường dẫn MỌI hướng dẫn cũ trên mạng vẫn
// ghi) không còn resolve được — Rolldown (bundler mặc định của Vite 8, xem `vite.config.ts`
// gốc dự án) báo lỗi "failed to resolve import" ngay khi build, không phải lỗi runtime.
// Đường ĐÚNG bỏ hẳn tiền tố `esm/vs` — exports map (`"./*": "./esm/vs/*.js"`) tự thêm lại.
import editorWorker from 'monaco-editor/editor/editor.worker?worker'
import { useEffect, useRef } from 'react'

import type { SqlCompletionItem } from '../../lib/sqlCompletions'

// SQL đã là một "basic language" đóng gói SẴN trong lõi `monaco-editor` — tokenizer của
// nó chạy trên main thread, không cần worker riêng, KHÔNG giống JSON/TypeScript/CSS/HTML
// (bốn ngôn ngữ có "rich language service" riêng, mỗi cái một worker). Autocomplete SQL
// (task sau) đăng ký qua `monaco.languages.registerCompletionItemProvider`, cũng không
// cần worker — nên ở đây CHỈ MỘT worker: `editor.worker`, dùng chung cho mọi tính năng
// lõi (bracket matching, mô hình tài liệu…) khi không có worker chuyên biệt nào khác.
//
// Gán ở SCOPE MODULE (chạy đúng MỘT lần khi chunk này được tải lần đầu — chunk này chỉ
// tải khi mở một item `sql_script`, xem `React.lazy` ở nơi gọi `SqlEditor`), không phải
// trong component: nhiều instance `SqlEditor` cùng lúc (nếu task sau cho mở nhiều tab)
// vẫn phải dùng chung một cấu hình worker.
self.MonacoEnvironment = {
  getWorker() {
    return new editorWorker()
  },
}

// `completionRegistry` ánh xạ MODEL (không phải component instance) -> danh sách gợi ý
// hiện hành của chính model đó. `monaco.languages.registerCompletionItemProvider` là
// một đăng ký TOÀN CỤC cho cả ngôn ngữ `sql` — gọi nó mỗi lần `SqlEditor` mount sẽ chồng
// nhiều provider (Monaco gộp kết quả của TẤT CẢ provider đã đăng ký), và người dùng thấy
// mỗi gợi ý lặp lại N lần với N lần mount. Đăng ký ĐÚNG MỘT LẦN ở scope module (cờ
// `completionProviderRegistered`), rồi để provider đó tra registry theo model tại thời
// điểm gợi ý được hỏi — đúng cách nhiều `SqlEditor` (nếu có nhiều tab sau này) không giẫm
// lên gợi ý của nhau, mỗi model giữ đúng danh sách của lakehouse nó đang trỏ tới.
const completionRegistry = new WeakMap<monaco.editor.ITextModel, SqlCompletionItem[]>()
let completionProviderRegistered = false

function ensureCompletionProviderRegistered() {
  if (completionProviderRegistered) return
  completionProviderRegistered = true
  monaco.languages.registerCompletionItemProvider('sql', {
    // Quyết định #5 của spec Giai đoạn 2c: gợi ý PHẲNG, không phân tích ngữ cảnh câu
    // lệnh (không cần biết con trỏ ở `FROM` hay `SELECT`) — `buildSqlCompletions`
    // (`lib/sqlCompletions.ts`) đã tính sẵn danh sách phẳng đó; provider ở đây chỉ có
    // việc TRA registry theo model và trả nguyên vẹn.
    provideCompletionItems(model, position) {
      const items = completionRegistry.get(model) ?? []
      const word = model.getWordUntilPosition(position)
      const range: monaco.IRange = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      }
      return {
        suggestions: items.map((item) => ({
          label: item.label,
          insertText: item.insertText,
          detail: item.detail,
          range,
          kind:
            item.kind === 'table'
              ? monaco.languages.CompletionItemKind.Struct
              : monaco.languages.CompletionItemKind.Field,
        })),
      }
    },
  })
}
ensureCompletionProviderRegistered()

// Nhãn owner cho `setModelMarkers` — Monaco nhóm marker theo owner để nhiều nguồn (đây,
// và về sau có thể một linter khác) không xoá nhầm marker của nhau khi gọi lại.
const SYNTAX_MARKER_OWNER = 'loom-sql-syntax'

export interface SqlErrorMarker {
  /** 1-based — khớp `loom_sql.errors.SqlError` (`sqlglot`, đã kiểm 1-based ở
   * `validate.py`) VÀ quy ước dòng/cột của chính Monaco, nên không cần dịch offset. */
  line: number
  column: number
  message: string
}

export interface SqlEditorProps {
  value: string
  onChange?: (value: string) => void
  readOnly?: boolean
  /** Gạch đỏ lỗi cú pháp — Giai đoạn 2c Phần A. Rỗng/`undefined` xoá hết marker cũ:
   * `SqlEditorPanel` gọi lại với mảng rỗng ngay khi người dùng gõ tiếp, để một marker cũ
   * không còn đúng chỗ sau khi nội dung đã đổi. */
  markers?: SqlErrorMarker[]
  /** Gợi ý bảng/cột cho autocomplete — Giai đoạn 2c Phần C. Tới từ lakehouse ĐANG CHỌN
   * (`SqlEditorPanel`/`useLakehouseSchema`), KHÔNG phải danh sách cứng — chứng minh đỏ 6. */
  completions?: SqlCompletionItem[]
}

/**
 * Trình soạn SQL bằng Monaco — chunk NẶNG, chỉ mount ở nơi gọi nó qua `React.lazy`, KHÔNG
 * BAO GIỜ qua `import` tĩnh (xem phép canh bundle `scripts/check-bundle-splitting.mjs`
 * và `ItemPage.tsx`, nơi thật sự lazy nó).
 *
 * Giai đoạn 2c: hiện ra, gõ được, gạch đỏ lỗi cú pháp đúng chỗ, và gợi ý bảng/cột. Chạy
 * query/huỷ/lưới kết quả là việc của `SqlEditorPanel`, xây TRÊN đúng ranh giới lazy-load
 * này — component này không biết gì về `loom-query`.
 */
export function SqlEditor({ value, onChange, readOnly = false, markers, completions }: SqlEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)

  // Dựng editor ĐÚNG MỘT LẦN khi mount. `value` ban đầu đọc trực tiếp từ prop lúc dựng —
  // đồng bộ những lần đổi SAU nằm ở effect riêng bên dưới, để không phải huỷ/dựng lại
  // toàn bộ editor (mất vị trí con trỏ, lịch sử undo) mỗi khi prop `value` đổi tham chiếu.
  useEffect(() => {
    if (!containerRef.current) return

    const editor = monaco.editor.create(containerRef.current, {
      value,
      language: 'sql',
      readOnly,
      automaticLayout: true,
      minimap: { enabled: false },
      fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
      fontSize: 13,
      scrollBeyondLastLine: false,
    })
    editorRef.current = editor

    const subscription = onChange
      ? editor.onDidChangeModelContent(() => onChange(editor.getValue()))
      : null

    return () => {
      subscription?.dispose()
      const model = editor.getModel()
      if (model) completionRegistry.delete(model)
      editor.dispose()
      editorRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dựng MỘT LẦN lúc mount, xem
    // giải thích ở trên; `value`/`onChange` sau đó đồng bộ ở các effect riêng.
  }, [])

  // Đồng bộ prop `value` VÀO editor khi nó đổi từ BÊN NGOÀI (vd. mở lại sau khi phục hồi
  // version khác) — nhưng không đụng tới khi chính người dùng đang gõ (`getValue()` đã
  // khớp `value` thì bỏ qua, tránh nhảy con trỏ về đầu ô mỗi lần `onChange` bắn ngược lên).
  useEffect(() => {
    const editor = editorRef.current
    if (editor && editor.getValue() !== value) editor.setValue(value)
  }, [value])

  // Gạch đỏ lỗi cú pháp — Phần A. `endColumn = column + 1`: server không trả độ dài của
  // token lỗi (`loom_sql.errors.SqlError` chỉ có điểm bắt đầu), nên gạch đúng MỘT ký tự
  // là đủ để người dùng thấy đúng vị trí — không đoán độ dài token để tránh gạch lố sang
  // chữ kế bên. Mảng rỗng/`undefined` xoá sạch marker cũ (đối số thứ ba `[]`).
  useEffect(() => {
    const model = editorRef.current?.getModel()
    if (!model) return
    monaco.editor.setModelMarkers(
      model,
      SYNTAX_MARKER_OWNER,
      (markers ?? []).map((marker) => ({
        severity: monaco.MarkerSeverity.Error,
        message: marker.message,
        startLineNumber: marker.line,
        startColumn: marker.column,
        endLineNumber: marker.line,
        endColumn: marker.column + 1,
      })),
    )
  }, [markers])

  // Đăng ký gợi ý của CHÍNH model này vào registry toàn cục — provider (đăng ký một lần
  // ở scope module, xem trên) tra lại đúng model đang được hỏi. Đổi lakehouse (schema
  // mới) chỉ cần đổi mục trong registry, không cần đăng ký lại provider.
  useEffect(() => {
    const model = editorRef.current?.getModel()
    if (!model) return
    completionRegistry.set(model, completions ?? [])
  }, [completions])

  return <div ref={containerRef} data-testid="sql-editor" className="h-full min-h-72" />
}
