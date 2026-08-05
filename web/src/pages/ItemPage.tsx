import { Link, useParams } from 'react-router'

import { describeError } from '../lib/useItemMutations'
import { useItem, useItemVersions, useRestoreVersion } from '../lib/useItem'

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

  if (isPending) {
    return (
      <div data-testid="item-skeleton" className="space-y-3">
        <div className="h-8 w-1/3 animate-pulse rounded bg-muted" />
        <div className="h-32 animate-pulse rounded bg-muted" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div role="alert" className="rounded-lg border border-line p-6">
        <p className="font-medium">Không mở được item</p>
        {/* Backend trả 404 cho cả "không tồn tại" và "không được đọc" (spec mục 4.5), nên
            nói thêm khả năng mất quyền là trung thực. */}
        <p className="mt-1 text-sm text-dim">{error?.message}</p>
        <p className="mt-2 text-sm text-dim">
          Item có thể đã bị xoá, hoặc bạn không còn quyền xem nó.
        </p>
        <Link to={`/workspaces/${workspaceId}/items`} className="mt-4 inline-block text-sm underline">
          Về cây item
        </Link>
      </div>
    )
  }

  const item = data.data

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/workspaces/${workspaceId}/items`} className="text-sm text-dim underline">
          ← Cây item
        </Link>
        {/* Nhãn nằm NGOÀI `h1`. Trong `h1` thì accessible name là chuỗi nối liền —
            "Xsql_scriptv1" — vì JSX bỏ khoảng trắng giữa các phần tử, và screen reader
            đọc nó thành một từ vô nghĩa. Đã gặp thật khi một test không khớp được tên. */}
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-lg font-medium">{item.display_name}</h1>
          <span className="rounded bg-muted px-2 py-0.5 text-xs text-dim">{item.type}</span>
          {/* Version hiện ra vì nó CHÍNH LÀ ETag: khi một lần sửa ăn 412, người dùng đọc
              được ở đây bản hiện tại là mấy. */}
          <span
            aria-label={`version ${item.version}`}
            className="rounded bg-muted px-2 py-0.5 text-xs text-dim"
          >
            v{item.version}
          </span>
        </div>
        <p className="mt-1 font-mono text-xs text-dim">
          {item.folder_path}
          {item.name}
        </p>
        {item.description && <p className="mt-2 text-sm">{item.description}</p>}
      </div>

      <section>
        <h2 className="mb-2 text-sm font-medium">Definition</h2>
        {/* Chỉ đọc, và cố ý: trình soạn thảo thuộc Giai đoạn 2. Một ô sửa được mà không
            lưu được thì tệ hơn một ô chỉ đọc. */}
        <pre className="overflow-auto rounded border border-line bg-muted p-3 text-xs">
          {JSON.stringify(item.definition, null, 2)}
        </pre>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium">Lịch sử version</h2>
        {versions.isPending && <p className="text-sm text-dim">Đang tải…</p>}
        {versions.error && (
          <p role="alert" className="text-sm text-dim">
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
                className="flex items-center gap-3 rounded border border-line px-3 py-2 text-sm"
              >
                <span className="w-10 shrink-0 font-mono text-xs">v{row.version}</span>
                <span className="min-w-0 flex-1 truncate">{row.display_name}</span>
                {row.change_note && (
                  <span className="truncate text-xs text-dim">{row.change_note}</span>
                )}
                <span className="shrink-0 text-xs text-dim">
                  {new Date(row.created_at).toLocaleString('vi-VN')}
                </span>
                <button
                  type="button"
                  // Version hiện tại không có gì để phục hồi: bấm nó chỉ sinh một version
                  // mới nội dung y hệt, cùng một dòng audit vô nghĩa.
                  disabled={row.version === item.version || restore.isPending}
                  onClick={() => restore.mutate(row.version)}
                  className="shrink-0 rounded border border-line px-2 py-0.5 text-xs disabled:opacity-40"
                >
                  Phục hồi
                </button>
              </li>
            ))}
          </ul>
        )}
        {restore.isError && restore.error && (
          <p role="alert" className="mt-2 text-sm text-dim">
            {describeError(restore.error)}
          </p>
        )}
        {/* Nói rõ `restore` LÀM GÌ. Người dùng tưởng nó ghi đè lịch sử là một hiểu nhầm
            đắt: nó sinh version MỚI mang nội dung cũ, nên không mất gì cả. */}
        <p className="mt-2 text-xs text-dim">
          Phục hồi tạo một version mới mang nội dung của version cũ — lịch sử không mất gì.
        </p>
      </section>
    </div>
  )
}
