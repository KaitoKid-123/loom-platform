import { useState } from 'react'
import { Link, useParams } from 'react-router'

import { ItemTypeIcon, typeLabel } from '../components/ItemTypeIcon'
import { PageHeader } from '../components/PageHeader'
// Monaco (qua `SqlEditor`) sống sau MỘT `React.lazy` — nhưng biên giới đó nằm TRONG
// `SqlEditorPanel.tsx` bây giờ (chỗ thật sự cần Monaco: chạy/huỷ/lưới kết quả/lưu/
// autocomplete, Giai đoạn 2c), không còn ở đây. `SqlEditorPanel` bản thân nó nhẹ (không
// import `monaco-editor`), nên `import` TĨNH nó ở đây không kéo Monaco vào chunk khởi
// đầu — phép canh bundle (`scripts/check-bundle-splitting.mjs`) khớp theo ĐƯỜNG DẪN
// NGUỒN `src/components/Editor/SqlEditor.tsx` trong manifest, không theo file nào gọi
// `import()`, nên di chuyển biên giới lazy vào trong không đổi module đích của phép canh.
import { SqlEditorPanel } from '../components/Editor/SqlEditorPanel'

import { describeError } from '../lib/useItemMutations'
import { useItem, useItemVersions, useRestoreVersion, useVersion } from '../lib/useItem'

/**
 * Trang chi tiết item, CHỈ ĐỌC ở Giai đoạn 1.
 *
 * Không có nó thì mọi cú bấm item trong Explorer và mọi Enter trong ⌘K đều dẫn tới trang
 * "không tìm thấy" — một hành trình vỡ, dù cả hai chỗ kia đều có test xanh. Trình soạn
 * thảo thật thuộc Giai đoạn 2; ở đây là metadata, definition, và lịch sử version.
 */
export function ItemPage() {
  const { workspaceId = '', itemId = '' } = useParams()
  const { data, isPending, error } = useItem(itemId)
  const versions = useItemVersions(itemId)
  const restore = useRestoreVersion(itemId, workspaceId)
  const [openVersion, setOpenVersion] = useState<number | null>(null)
  const opened = useVersion(itemId, openVersion)

  if (isPending) {
    return (
      <div data-testid="item-skeleton" className="space-y-3 p-5">
        <div className="h-8 w-1/3 animate-pulse rounded bg-hover" />
        <div className="h-32 animate-pulse rounded bg-hover" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div role="alert" className="m-5 rounded-md border border-line bg-surface p-6">
        <p className="font-medium">Could not open this item</p>
        {/* Backend trả 404 cho cả "không tồn tại" và "không được đọc" (spec mục 4.5), nên
            nói thêm khả năng mất quyền là trung thực. */}
        <p className="mt-1 text-[13px] text-dim">{error?.message}</p>
        <p className="mt-2 rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
          The item may have been deleted, or you may no longer have permission to see it.
        </p>
        <Link to={`/workspaces/${workspaceId}/items`} className="mt-4 inline-block text-[13px] text-accent underline">
          Back to all items
        </Link>
      </div>
    )
  }

  const item = data.data

  return (
    <>
      <PageHeader
        crumbs={[
          { label: 'Workspaces', to: '/' },
          { label: 'All items', to: `/workspaces/${workspaceId}/items` },
          { label: item.display_name },
        ]}
        title={
          <span className="flex items-center gap-2">
            <ItemTypeIcon type={item.type} size={18} />
            {item.display_name}
          </span>
        }
        actions={
          <>
            <span className="rounded bg-raised px-1.5 py-0.5 text-[12px] text-dim">
              {typeLabel(item.type)}
            </span>
            {/* Version hiện ra vì nó CHÍNH LÀ ETag: khi một lần sửa ăn 412, người dùng
                đọc được ở đây bản hiện tại là mấy. */}
            <span
              aria-label={`version ${item.version}`}
              className="tabular rounded bg-raised px-1.5 py-0.5 text-[12px] text-dim"
            >
              v{item.version}
            </span>
          </>
        }
      />

    <div className="space-y-6 p-5">
      <p className="font-mono text-[12px] text-faint">
        {item.folder_path}
        {item.name}
      </p>
      {item.description && <p className="text-[13px]">{item.description}</p>}

      <section>
        <h2 className="mb-2 text-[12px] font-semibold uppercase tracking-wider text-dim">Definition</h2>
        {item.type === 'sql_script' ? (
          // `sql_script` mở bằng Monaco chứ không JSON thô, VÀ dùng được: chạy, huỷ,
          // lưới kết quả, lưu thành version, autocomplete — `SqlEditorPanel` (Giai đoạn
          // 2c) xây trên đúng ranh giới lazy-load Monaco mà task trước đã dựng.
          <SqlEditorPanel item={item} etag={data.etag} workspaceId={workspaceId} />
        ) : (
          // Chỉ đọc, và cố ý cho MỌI loại khác: trình soạn thảo cho chúng thuộc giai đoạn
          // sau. Một ô sửa được mà không lưu được thì tệ hơn một ô chỉ đọc.
          <pre className="overflow-auto rounded-md border border-line bg-surface p-3 font-mono text-[12px] leading-relaxed">
            {JSON.stringify(item.definition, null, 2)}
          </pre>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-[12px] font-semibold uppercase tracking-wider text-dim">Version history</h2>
        {versions.isPending && <p className="text-[13px] text-dim">Loading…</p>}
        {versions.error && (
          <p role="alert" className="text-[13px] text-dim">
            {versions.error.message}
          </p>
        )}
        {versions.data && (
          <ul className="space-y-1">
            {/* `?? []` chứ không `.items.map` trần: một phản hồi thiếu mảng `items` —
                API triển khai dở, một proxy chen vào — làm `.map` ném và React Router
                thay CẢ TRANG bằng trang lỗi của nó. Đã gặp thật khi viết test. */}
            {(versions.data.items ?? []).map((row) => (
              <li
                key={row.version}
                className="flex items-center gap-3 rounded border border-line bg-surface px-3 py-1.5 text-[13px]"
              >
                <button
                  type="button"
                  // Mở ra để ĐỌC nội dung cũ trước khi quyết định phục hồi. Không có nó
                  // thì "Restore" là một nút bấm mù.
                  onClick={() => setOpenVersion(openVersion === row.version ? null : row.version)}
                  aria-expanded={openVersion === row.version}
                  className="flex min-w-0 flex-1 items-center gap-3 text-left hover:text-accent"
                >
                  <span className="tabular w-9 shrink-0 font-mono text-[12px] text-dim">
                    v{row.version}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{row.display_name}</span>
                </button>
                {row.change_note && (
                  <span className="truncate text-[12px] text-faint">{row.change_note}</span>
                )}
                <span className="tabular shrink-0 text-[12px] text-dim">
                  {new Date(row.created_at).toLocaleString('en-GB')}
                </span>
                <button
                  type="button"
                  // Version hiện tại không có gì để phục hồi: bấm nó chỉ sinh một version
                  // mới nội dung y hệt, cùng một dòng audit vô nghĩa.
                  disabled={row.version === item.version || restore.isPending}
                  onClick={() => restore.mutate(row.version)}
                  className="shrink-0 rounded border border-line-strong px-2 py-0.5 text-[12px] text-dim transition-colors hover:bg-hover hover:text-ink disabled:opacity-40"
                >
                  Restore
                </button>
              </li>
            ))}
            {openVersion !== null && (
              <li className="rounded border border-accent-line bg-raised p-3">
                <p className="mb-2 text-[12px] font-medium text-dim">
                  Definition at v{openVersion}
                </p>
                {opened.isPending && <p className="text-[13px] text-dim">Loading…</p>}
                {opened.error && (
                  <p role="alert" className="text-[13px] text-danger">
                    {opened.error.message}
                  </p>
                )}
                {opened.data && (
                  <pre className="overflow-auto rounded border border-line bg-surface p-3 font-mono text-[12px] leading-relaxed">
                    {JSON.stringify(opened.data.definition, null, 2)}
                  </pre>
                )}
              </li>
            )}
          </ul>
        )}
        {restore.isError && restore.error && (
          <p role="alert" className="mt-2 rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
            {describeError(restore.error)}
          </p>
        )}
        {/* Nói rõ `restore` LÀM GÌ. Người dùng tưởng nó ghi đè lịch sử là một hiểu nhầm
            đắt: nó sinh version MỚI mang nội dung cũ, nên không mất gì cả. */}
        <p className="mt-2 text-[12px] text-dim">
          Restoring creates a new version carrying the old content — nothing is lost from the
          history.
        </p>
      </section>
    </div>
    </>
  )
}
