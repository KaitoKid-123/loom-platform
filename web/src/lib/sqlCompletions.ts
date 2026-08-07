import type { LakehouseSchemaResponse } from './lakehouseSchema'

export interface SqlCompletionItem {
  label: string
  insertText: string
  kind: 'table' | 'column'
  detail?: string
}

/**
 * Gợi ý tên bảng/cột PHẲNG cho autocomplete SQL — quyết định #5 của spec Giai đoạn 2c:
 * "chạy được + gợi tên bảng/cột", KHÔNG phân tích ngữ cảnh câu lệnh (không cần biết con
 * trỏ đang ở `FROM` hay `SELECT`). Cố làm hơn ở giai đoạn này là mở một hố — xem spec.
 *
 * Tách khỏi Monaco hoàn toàn (không import `monaco-editor`) để kiểm được bằng dữ liệu
 * thuần, không cần dựng registry/model giả — `SqlEditor.tsx` là nơi DUY NHẤT map kết
 * quả này sang `monaco.languages.CompletionItem`.
 *
 * Bảng: `namespace.table` (khớp cách người dùng phải viết tên bảng hai phần — xem
 * `loom_query.authz._resolve_tables`: tên bảng KHÔNG namespace bị từ chối). Cột: chỉ
 * TÊN cột (không tiền tố bảng) — sqlglot/DuckDB không đòi định danh cột phải có tiền
 * tố, và gợi ý ngắn dễ gõ hơn.
 *
 * Cột khử trùng lặp theo TÊN: nhiều bảng cùng có cột `id`/`created_at` không cần lặp
 * lại gợi ý — `detail` (kiểu dữ liệu) giữ của lần gặp ĐẦU TIÊN, đủ để người dùng phân
 * biệt, không cần biết cột đó thuộc bảng nào để gõ được nó.
 */
export function buildSqlCompletions(
  schema: LakehouseSchemaResponse | null | undefined,
): SqlCompletionItem[] {
  const namespaces = schema?.namespaces ?? []
  const items: SqlCompletionItem[] = []
  const seenColumns = new Set<string>()

  for (const ns of namespaces) {
    for (const table of ns.tables ?? []) {
      const qualified = `${ns.name}.${table.name}`
      items.push({ label: qualified, insertText: qualified, kind: 'table' })

      for (const col of table.columns ?? []) {
        if (seenColumns.has(col.name)) continue
        seenColumns.add(col.name)
        items.push({ label: col.name, insertText: col.name, kind: 'column', detail: col.type })
      }
    }
  }

  return items
}
