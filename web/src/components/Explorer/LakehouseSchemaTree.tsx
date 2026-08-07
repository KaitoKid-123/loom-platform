import { useState } from 'react'

import { type NamespaceNode, buildLakehouseTree } from '../../lib/lakehouseSchema'
import { useLakehouseSchema } from '../../lib/useLakehouseSchema'

interface Props {
  lakehouseId: string
}

/**
 * Cây namespace -> bảng -> cột trong panel trái, khi item đang mở là một lakehouse.
 *
 * Tải bằng `depth=tables` trước (~13ms, ~4KB cho một lakehouse 200 bảng x 30 cột — số đo
 * thật, xem docstring `loom_query.lakehouse_schema`), và CHỈ gọi `depth=columns`
 * (~1,5s, ~221KB CÙNG lakehouse đó) khi người dùng thật sự mở một bảng ra xem cột. Tải
 * cả cây đầy đủ ngay từ đầu rồi lọc phía client là bắt MỌI người chờ 1,5 giây cho thứ họ
 * chưa hỏi — kể cả người chỉ ghé qua để đổi tên lakehouse.
 *
 * Không có API "lấy cột của MỘT bảng" — `depth=columns` trả cột cho CẢ lakehouse trong
 * một lời gọi (xem module docstring `lakehouse_schema.py`: mỗi bảng là một round trip
 * `load_table` riêng, không có cách hàng loạt). Nên bảng THỨ HAI được mở dùng lại đúng
 * dữ liệu bảng đầu đã kéo về — không gọi lại.
 */
export function LakehouseSchemaTree({ lakehouseId }: Props) {
  const [openTables, setOpenTables] = useState<Set<string>>(new Set())
  const [columnsRequested, setColumnsRequested] = useState(false)

  const tables = useLakehouseSchema(lakehouseId, 'tables')
  const columns = useLakehouseSchema(lakehouseId, 'columns', columnsRequested)

  // Ưu tiên phản hồi `columns`: nó là SIÊU TẬP của `tables` (cùng namespace/bảng, kèm
  // thêm cột), nên một khi đã có, mọi bảng — kể cả bảng người dùng CHƯA mở — đều có cột
  // sẵn trong cache, và mở bảng thứ hai không cần gọi lại.
  const raw = columns.data ?? tables.data
  const namespaces = raw ? buildLakehouseTree(raw) : null

  const toggleTable = (key: string) => {
    setOpenTables((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
    // Bật MỘT LẦN, không bao giờ tắt lại: một khi đã trả tiền cho `depth=columns`, dữ
    // liệu đó phục vụ MỌI bảng, nên không có lý do gọi lại kể cả khi người dùng đóng rồi
    // mở lại bảng khác.
    setColumnsRequested(true)
  }

  return (
    <div className="border-t border-line pt-2">
      <p className="px-3 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
        Tables
      </p>

      {tables.isPending && (
        // Skeleton theo HÌNH của một cây namespace/bảng, không spinner toàn khối — quy
        // tắc bắt buộc spec 7.4.
        <div data-testid="lakehouse-tree-skeleton" className="space-y-1.5 px-3 pb-2">
          <div className="h-3 w-2/5 animate-pulse rounded bg-hover" />
          <div className="ml-3 h-3 w-3/5 animate-pulse rounded bg-hover" />
          <div className="ml-3 h-3 w-2/4 animate-pulse rounded bg-hover" />
        </div>
      )}

      {tables.error && (
        <p role="alert" className="px-3 pb-2 text-[12px] text-danger">
          {/* Nguyên văn thông báo của server — spec 7.4. Xem `useLakehouseSchema`: dùng
              `apiGetWithEtag` (chứ không `apiGet`) chính là để `error.message` mang
              được `detail` thật của `SchemaForbidden`, không chỉ mã trạng thái. */}
          {tables.error.message}
        </p>
      )}

      {namespaces && namespaces.length === 0 && (
        <div className="px-3 pb-3 text-[12px] text-dim">
          <p className="font-medium text-ink">This lakehouse has no tables yet.</p>
          {/* Bước tiếp theo, không chỉ nói rỗng — quy tắc bắt buộc spec 7.4. */}
          <p className="mt-1 text-faint">
            Run a pipeline or a SQL script that writes to it to populate this list.
          </p>
        </div>
      )}

      {namespaces && namespaces.length > 0 && (
        <ul>
          {namespaces.map((ns) => (
            <NamespaceRow
              key={ns.name}
              namespace={ns}
              openTables={openTables}
              onToggleTable={toggleTable}
              columnsRequested={columnsRequested}
              columnsPending={columns.isPending}
              columnsError={columnsRequested ? columns.error : null}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function NamespaceRow({
  namespace,
  openTables,
  onToggleTable,
  columnsPending,
  columnsError,
}: {
  namespace: NamespaceNode
  openTables: Set<string>
  onToggleTable: (key: string) => void
  columnsRequested: boolean
  columnsPending: boolean
  columnsError: Error | null
}) {
  return (
    <li>
      <p className="flex items-center gap-1.5 px-3 py-1 text-[12px] font-medium text-dim">
        <NamespaceIcon />
        <span className="truncate">{namespace.name}</span>
      </p>
      <ul>
        {namespace.tables.map((table) => {
          const key = `${namespace.name}.${table.name}`
          const open = openTables.has(key)
          return (
            <li key={key}>
              <button
                type="button"
                onClick={() => onToggleTable(key)}
                aria-expanded={open}
                className="flex w-full items-center gap-1.5 py-1 pl-6 pr-3 text-left text-[13px] hover:bg-hover"
              >
                <Chevron open={open} />
                <TableIcon />
                <span className="truncate">{table.name}</span>
              </button>
              {open && (
                <ul className="pb-1">
                  {table.columns === null && columnsError && (
                    <li role="alert" className="py-0.5 pl-12 pr-3 text-[12px] text-danger">
                      {columnsError.message}
                    </li>
                  )}
                  {table.columns === null && !columnsError && columnsPending && (
                    <li className="py-0.5 pl-12 pr-3 text-[12px] text-faint">Loading columns…</li>
                  )}
                  {table.columns?.map((col) => (
                    <li
                      key={col.name}
                      className="flex items-center justify-between gap-2 py-0.5 pl-12 pr-3 font-mono text-[11px] text-dim"
                    >
                      <span className="truncate">{col.name}</span>
                      <span className="shrink-0 text-faint">{col.type}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </li>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className={`shrink-0 text-faint transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path d="M5 3l6 5-6 5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function NamespaceIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden className="shrink-0 text-faint">
      <path
        d="M2 4.5a1 1 0 0 1 1-1h3.3l1.4 1.6h5.3a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-7.6Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function TableIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden className="shrink-0 text-type-lakehouse">
      <path
        d="M2.5 3.5h11v9h-11zM2.5 6.7h11M2.5 9.9h11M6.2 3.5v9.2M10 3.5v9.2"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
