import * as monaco from 'monaco-editor'
// LƯU Ý ĐO ĐƯỢC (bản thử ở `/tmp/…/monaco-experiment`, `npm run build` thật, không đoán):
// monaco-editor 0.56 thêm `exports` map trong package.json giới hạn deep-import.
// `monaco-editor/esm/vs/editor/editor.worker` (đường dẫn MỌI hướng dẫn cũ trên mạng vẫn
// ghi) không còn resolve được — Rolldown (bundler mặc định của Vite 8, xem `vite.config.ts`
// gốc dự án) báo lỗi "failed to resolve import" ngay khi build, không phải lỗi runtime.
// Đường ĐÚNG bỏ hẳn tiền tố `esm/vs` — exports map (`"./*": "./esm/vs/*.js"`) tự thêm lại.
import editorWorker from 'monaco-editor/editor/editor.worker?worker'
import { useEffect, useRef } from 'react'

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

export interface SqlEditorProps {
  value: string
  onChange?: (value: string) => void
  readOnly?: boolean
}

/**
 * Trình soạn SQL bằng Monaco — chunk NẶNG, chỉ mount ở nơi gọi nó qua `React.lazy`, KHÔNG
 * BAO GIỜ qua `import` tĩnh (xem phép canh bundle `scripts/check-bundle-splitting.mjs`
 * và `ItemPage.tsx`, nơi thật sự lazy nó).
 *
 * Ở Giai đoạn 2c này chỉ cần HIỆN RA và GÕ ĐƯỢC — chạy query, huỷ, lưới kết quả,
 * autocomplete là việc của task sau, xây TRÊN đúng ranh giới lazy-load này.
 */
export function SqlEditor({ value, onChange, readOnly = false }: SqlEditorProps) {
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

  return <div ref={containerRef} data-testid="sql-editor" className="h-full min-h-72" />
}
