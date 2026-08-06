import { Link, useNavigate } from 'react-router'

import { PageHeader } from '../components/PageHeader'
import { atLeast, useWorkspaces } from '../lib/useWorkspaces'

/** Chữ cái đầu của workspace, làm ô màu thay cho ảnh — Fabric cũng làm đúng thế. */
function badge(name: string): string {
  return name.trim().slice(0, 2).toUpperCase() || '··'
}

/**
 * Màu ô suy ra TỪ ID, không phải ngẫu nhiên: cùng một workspace phải có cùng màu ở mọi
 * lần tải và với mọi người, nếu không màu không giúp nhận diện được gì.
 */
const TINTS = [
  'var(--color-type-lakehouse)',
  'var(--color-type-pipeline)',
  'var(--color-type-sql)',
  'var(--color-type-connection)',
]
function tint(id: string): string {
  let hash = 0
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return TINTS[hash % TINTS.length]
}

export function WorkspaceListPage() {
  const { data, isPending, error } = useWorkspaces()
  const navigate = useNavigate()

  const items = data?.items ?? []

  return (
    <>
      <PageHeader crumbs={[{ label: 'Workspaces' }]} title="Workspaces" />

      <div className="p-5">
        {isPending && (
          // Skeleton theo HÌNH của nội dung sắp tới, không phải spinner toàn trang — quy
          // tắc bắt buộc của spec mục 7.4. Spinner làm cả khung nhảy một nhịp rồi nhảy
          // lại, và người dùng mất chỗ mắt đang đặt.
          <div
            data-testid="workspace-skeleton"
            className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
          >
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-24 animate-pulse rounded-md border border-line bg-surface" />
            ))}
          </div>
        )}

        {error && (
          <div role="alert" className="rounded-md border border-line bg-surface p-6">
            <p className="font-medium">Could not load workspaces</p>
            {/* Thông báo của server, nguyên văn. Thay bằng "Có lỗi" là bỏ đi thứ duy nhất
                giúp người dùng hoặc người vận hành biết chuyện gì vừa xảy ra. */}
            <p className="mt-1 text-[13px] text-dim">{error.message}</p>
          </div>
        )}

        {data && items.length === 0 && (
          <div className="rounded-md border border-dashed border-line-strong bg-surface p-12 text-center">
            <p className="text-[14px] font-medium">You do not have access to any workspace</p>
            {/* Trạng thái rỗng phải nói BƯỚC TIẾP THEO, không chỉ nói là rỗng. Và nhắc
                tới nhóm vì vai trò gán cho nhóm cấp quyền y như gán cho cá nhân (Task
                25) — người dùng không tự biết điều đó. */}
            <p className="mx-auto mt-1.5 max-w-sm text-[13px] text-dim">
              Ask an administrator to grant you a role, or to grant one to a group you belong
              to.
            </p>
          </div>
        )}

        {items.length > 0 && (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((ws) => (
              <li
                key={ws.id}
                className="group rounded-md border border-line bg-surface transition-shadow hover:border-line-strong hover:shadow-sm"
              >
                <div className="flex items-start gap-3 p-3.5">
                  <span
                    aria-hidden
                    style={{ backgroundColor: tint(ws.id) }}
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded text-[12px] font-semibold text-white"
                  >
                    {badge(ws.display_name)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/workspaces/${ws.id}/items`}
                      className="block truncate text-[14px] font-semibold hover:text-accent hover:underline"
                    >
                      {ws.display_name}
                    </Link>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-faint">{ws.name}</p>
                    {ws.description && (
                      <p className="mt-1 line-clamp-2 text-[12px] text-dim">{ws.description}</p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 border-t border-line px-3.5 py-2">
                  <span className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-dim">
                    {ws.my_role}
                  </span>
                  <div className="flex-1" />
                  {/* Ẩn nút mà server sẽ từ chối. Chặn ở server là bắt buộc và đã có; ẩn
                      ở đây là để người dùng không bấm rồi ăn 403 mà không hiểu vì sao. */}
                  {atLeast(ws.my_role, 'contributor') && (
                    <button
                      type="button"
                      // `?new=1` chứ không state React: hộp thoại tạo item mở được bằng
                      // đường dẫn, nên nó deep-link và F5 được (spec mục 7.4).
                      onClick={() => navigate(`/workspaces/${ws.id}/items?new=1`)}
                      className="rounded border border-line-strong px-2 py-0.5 text-[12px] text-dim opacity-0 transition-opacity hover:bg-hover hover:text-ink focus-visible:opacity-100 group-hover:opacity-100"
                    >
                      New item
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  )
}
