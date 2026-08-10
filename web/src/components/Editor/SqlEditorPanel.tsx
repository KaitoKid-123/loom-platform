import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'

import { ToolbarButton } from '../PageHeader'

import { buildSqlCompletions } from '../../lib/sqlCompletions'
import type { ItemDetail } from '../../lib/useItem'
import { describeError, useUpdateItemDefinition } from '../../lib/useItemMutations'
import { useItems } from '../../lib/useItems'
import { useLakehouseSchema } from '../../lib/useLakehouseSchema'
import {
  QueryPermissionError,
  QuerySubmitError,
  QuerySyntaxError,
} from '../../lib/queryApi'
import { useRunQuery } from '../../lib/useRunQuery'
import { ResultGrid } from './ResultGrid'
import type { SqlErrorMarker } from './SqlEditor'

// `React.lazy`, KHÔNG `import` tĩnh — Monaco nặng 2-5MB (xem `SqlEditor.tsx` và phép
// canh bundle `scripts/check-bundle-splitting.mjs`, khớp theo ĐƯỜNG DẪN NGUỒN của
// module, không theo file nào gọi `import()`). Chuyển biên giới lazy từ `ItemPage.tsx`
// vào đây (chỗ thật sự dùng Monaco) không đổi module đích, nên phép canh vẫn đúng.
const SqlEditor = lazy(() => import('./SqlEditor').then((m) => ({ default: m.SqlEditor })))

function SqlEditorSkeleton() {
  return (
    <div
      data-testid="sql-editor-skeleton"
      className="min-h-72 space-y-2 rounded-md border border-line bg-surface p-3"
    >
      {[92, 68, 76, 40, 84, 55].map((width, i) => (
        <div key={i} className="h-3 animate-pulse rounded bg-hover" style={{ width: `${width}%` }} />
      ))}
    </div>
  )
}

interface Props {
  item: ItemDetail
  etag: string | null
  workspaceId: string
}

/**
 * SQL Editor "dùng được" — Giai đoạn 2c: chạy, huỷ, lưới kết quả, lưu thành version,
 * autocomplete. Xây TRÊN `SqlEditor.tsx` (Monaco, lazy) và `useRunQuery`/`queryApi`
 * (vòng đời bất đồng bộ) mà KHÔNG chạm gì tới cách hai thứ đó tự bảo đảm đúng.
 *
 * Lakehouse chạy CÙNG là lựa chọn của người dùng, không phải trường trong định nghĩa
 * `sql_script` (`SqlScriptDefinition` chỉ có `sql`) — lưu trong QUERY STRING (`?lakehouse=`,
 * quy tắc bắt buộc spec 7.4 "URL là state") để chọn một lần không mất khi F5, và một câu
 * SQL vẫn dùng lại được với một lakehouse KHÁC mà không phải sửa định nghĩa item.
 */
export function SqlEditorPanel({ item, etag, workspaceId }: Props) {
  const [searchParams, setSearchParams] = useSearchParams()
  const lakehouseId = searchParams.get('lakehouse') ?? ''

  function selectLakehouse(id: string) {
    const next = new URLSearchParams(searchParams)
    if (id) next.set('lakehouse', id)
    else next.delete('lakehouse')
    setSearchParams(next, { replace: true })
  }

  const lakehouses = useItems(workspaceId, 'lakehouse')
  // `depth=columns` NGAY, không lazy theo bảng như `LakehouseSchemaTree`: autocomplete
  // không biết trước người dùng sắp gõ bảng nào, nên cần TOÀN BỘ cây cột một lần — số đo
  // thật (~1,5s/~221KB cho 200 bảng x 30 cột, docstring `loom_query.lakehouse_schema`)
  // là cái giá TRẢ MỘT LẦN khi chọn lakehouse, và TanStack Query cache nó (đọc
  // `useLakehouseSchema`: khoá theo `[lakehouseId, depth]`) — gõ tiếp KHÔNG gọi lại
  // (chứng minh đỏ 7).
  const schema = useLakehouseSchema(lakehouseId, 'columns')
  const completions = buildSqlCompletions(schema.data)

  const initialSql = typeof item.definition.sql === 'string' ? item.definition.sql : ''
  const [sql, setSql] = useState(initialSql)
  const [markers, setMarkers] = useState<SqlErrorMarker[]>([])
  // Đồng bộ nội dung TỪ server khi `version` đổi (phục hồi version cũ, hoặc chính lần
  // lưu của mình vừa xong) — KHÔNG đồng bộ mỗi lần `item` đổi tham chiếu (một refetch
  // giữ nguyên version thì không có gì để đồng bộ, và làm vậy sẽ nuốt mất nội dung
  // người dùng đang gõ dở). Chứng minh đỏ 5 của Phần B dựa vào đúng effect này.
  const syncedVersionRef = useRef(item.version)
  useEffect(() => {
    if (item.version === syncedVersionRef.current) return
    syncedVersionRef.current = item.version
    setSql(typeof item.definition.sql === 'string' ? item.definition.sql : '')
    setMarkers([])
  }, [item.version, item.definition])

  const run = useRunQuery()
  const save = useUpdateItemDefinition(item.id, workspaceId)

  // Lỗi cú pháp của LƯỢT CHẠY GẦN NHẤT trở thành marker trong Monaco — chứng minh đỏ 3
  // của Phần A ("gạch ĐÚNG dòng/cột server báo"). Marker CŨ bị xoá ngay khi người dùng
  // gõ tiếp (`handleChange`) vì nó không còn đúng chỗ sau khi nội dung đã đổi.
  useEffect(() => {
    if (run.state.submitError instanceof QuerySyntaxError) {
      setMarkers(run.state.submitError.issues)
    }
  }, [run.state.submitError])

  function handleChange(next: string) {
    setSql(next)
    setMarkers([])
  }

  function handleSave() {
    save.mutate({ etag: etag ?? undefined, definition: { ...item.definition, sql } })
  }

  const canRun = lakehouseId !== '' && run.state.phase !== 'submitting' && run.state.phase !== 'running'

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="sql-lakehouse" className="text-[12px] text-dim">
          Run against
        </label>
        <select
          id="sql-lakehouse"
          value={lakehouseId}
          onChange={(e) => selectLakehouse(e.target.value)}
          className="h-7 rounded border border-line-strong bg-surface px-1.5 text-[13px]"
        >
          <option value="">Select a lakehouse…</option>
          {(lakehouses.data?.items ?? []).map((lh) => (
            <option key={lh.id} value={lh.id}>
              {lh.display_name}
            </option>
          ))}
        </select>

        <div className="flex-1" />

        {run.state.phase === 'running' && (
          <ToolbarButton onClick={() => run.cancel()}>Cancel</ToolbarButton>
        )}
        <ToolbarButton
          variant="primary"
          disabled={!canRun}
          onClick={() => run.run(lakehouseId, sql)}
        >
          {run.state.phase === 'submitting' || run.state.phase === 'running' ? 'Running…' : 'Run'}
        </ToolbarButton>
        <ToolbarButton disabled={save.isPending} onClick={handleSave}>
          {save.isPending ? 'Saving…' : 'Save'}
        </ToolbarButton>
      </div>

      <Suspense fallback={<SqlEditorSkeleton />}>
        <SqlEditor value={sql} onChange={handleChange} markers={markers} completions={completions} />
      </Suspense>

      {save.isError && save.error && (
        <p role="alert" className="rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
          {describeError(save.error)}
        </p>
      )}

      {run.state.submitError && (
        <SubmitErrorBanner error={run.state.submitError} />
      )}

      {run.state.phase === 'cancelled' && (
        <p role="status" className="text-[13px] text-dim">
          Query cancelled.
        </p>
      )}

      {run.state.phase === 'failed' && (
        <div
          role="alert"
          className={`rounded border px-2.5 py-1.5 text-[13px] ${
            run.state.overLimit
              ? 'border-line bg-warn-soft text-warn'
              : 'border-line bg-danger-soft text-danger'
          }`}
        >
          <p className="font-medium">{run.state.overLimit ? 'Query too large' : 'Query failed'}</p>
          <p>{run.state.error}</p>
        </div>
      )}

      {run.state.phase === 'succeeded' && run.state.result && (
        <ResultGrid
          columns={run.state.result.columns}
          rows={run.state.result.rows}
          truncated={run.state.result.truncated}
          rowCount={run.state.result.rowCount}
        />
      )}
    </div>
  )
}

function SubmitErrorBanner({ error }: { error: Error }) {
  if (error instanceof QueryPermissionError) {
    return (
      <p role="alert" className="rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
        Permission denied — {error.message}
      </p>
    )
  }
  if (error instanceof QuerySyntaxError) {
    return (
      <div role="alert" className="rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
        <p className="font-medium">The SQL failed to parse</p>
        <ul className="mt-1 list-inside list-disc">
          {error.issues.map((issue, i) => (
            <li key={i}>
              Line {issue.line}, column {issue.column}: {issue.message}
            </li>
          ))}
        </ul>
      </div>
    )
  }
  if (error instanceof QuerySubmitError) {
    return (
      <p role="alert" className="rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
        {error.message}
      </p>
    )
  }
  return (
    <p role="alert" className="rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
      {error.message}
    </p>
  )
}
