import { useParams, useSearchParams } from 'react-router'

import { ItemTable } from '../components/Explorer/ItemTable'
import { NewItemDialog } from '../components/Explorer/NewItemDialog'
import { typeLabel } from '../components/ItemTypeIcon'
import { type Crumb, PageHeader, ToolbarButton } from '../components/PageHeader'
import { PermissionsDialog } from '../components/PermissionsDialog'
import { buildTree, folderCrumbs, nodeAt, normaliseFolder } from '../lib/folderTree'
import { useItems } from '../lib/useItems'

// Đúng bốn loại của backend (`ItemType` trong `item_definitions.py`). Thêm một loại
// không tồn tại vào đây thì bộ lọc gửi `?type=` lạ và ăn 422 — backend liệt kê các loại
// hợp lệ trong thân phản hồi từ cửa chặn 1b, nhưng vẫn là một lỗi vô ích.
const TYPES = ['lakehouse', 'connection', 'pipeline', 'sql_script'] as const

export function ExplorerPage() {
  const { workspaceId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const type = searchParams.get('type') ?? undefined
  const folder = normaliseFolder(searchParams.get('folder'))
  // Hộp thoại mở QUA URL: `?new=1` deep-link và sống qua F5 (spec mục 7.4). State React
  // sẽ mất khi tải lại, và một hộp thoại đã điền nửa mà biến mất là mất việc.
  const creating = searchParams.get('new') === '1'
  const managingPerms = searchParams.get('perms') === '1'

  /** Sửa MỘT tham số, giữ nguyên các tham số khác — xoá cả query string sẽ làm người
   *  dùng mất bộ lọc chỉ vì họ mở rồi đóng một hộp thoại. */
  const patch = (mutate: (next: URLSearchParams) => void, replace = true) => {
    const next = new URLSearchParams(searchParams)
    mutate(next)
    setSearchParams(next, { replace })
  }

  const { data, isPending, error } = useItems(workspaceId, type)

  const tree = data ? buildTree(data.items) : null
  const current = tree ? nodeAt(tree, folder) : null
  const path = folderCrumbs(folder)

  const crumbs: Crumb[] = [
    { label: 'Workspaces', to: '/' },
    {
      label: 'All items',
      to: path.length > 0 ? `/workspaces/${workspaceId}/items` : undefined,
    },
    ...path.map((c, index) => ({
      label: c.name,
      to:
        index === path.length - 1
          ? undefined
          : `/workspaces/${workspaceId}/items?folder=${encodeURIComponent(c.path)}`,
    })),
  ]

  return (
    <>
      <PageHeader
        crumbs={crumbs}
        title={path.at(-1)?.name ?? 'All items'}
        actions={
          <>
            {/* ĐÚNG MỘT nút primary trên thanh: một thanh công cụ mà mọi nút trông giống
                nhau thì không nút nào nổi lên là việc chính, và người dùng phải đọc hết
                mới biết bấm gì. */}
            <ToolbarButton variant="primary" onClick={() => patch((n) => n.set('new', '1'), false)}>
              <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden>
                <path
                  d="M8 3.2v9.6M3.2 8h9.6"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                />
              </svg>
              New item
            </ToolbarButton>
            <ToolbarButton onClick={() => patch((n) => n.set('perms', '1'), false)}>
              Permissions
            </ToolbarButton>
            <div className="ml-1 flex items-center gap-1.5">
              <label className="text-[12px] text-dim" htmlFor="type-filter">
                Type
              </label>
              <select
                id="type-filter"
                value={type ?? ''}
                // Bộ lọc vào QUERY STRING, không vào state React: người dùng gửi được
                // đường dẫn và F5 không mất chỗ đang đứng. Quy tắc bắt buộc spec 7.4.
                //
                // `replace` để đổi bộ lọc năm lần không nhồi năm mục vào history — nút
                // Back phải đưa người dùng về chỗ họ ĐẾN TỪ, không lùi qua từng lần lọc.
                onChange={(e) =>
                  patch((n) => (e.target.value ? n.set('type', e.target.value) : n.delete('type')))
                }
                className="h-7 rounded border border-line-strong bg-surface px-1.5 text-[13px]"
              >
                <option value="">All</option>
                {TYPES.map((t) => (
                  <option key={t} value={t}>
                    {typeLabel(t)}
                  </option>
                ))}
              </select>
            </div>
          </>
        }
      />

      <div className="p-5">
        {isPending && (
          <div data-testid="items-skeleton" className="space-y-px">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="h-8 animate-pulse rounded bg-hover" />
            ))}
          </div>
        )}

        {error && (
          <div role="alert" className="rounded-md border border-line bg-surface p-6">
            <p className="font-medium">Could not load items</p>
            <p className="mt-1 text-[13px] text-dim">{error.message}</p>
          </div>
        )}

        {data && data.items.length === 0 && (
          <div className="rounded-md border border-dashed border-line-strong bg-surface p-12 text-center">
            {/* Phân biệt "workspace rỗng" với "bộ lọc không khớp gì". Gộp hai câu làm
                người dùng vừa đặt bộ lọc tưởng workspace của mình trống. */}
            <p className="text-[14px] font-medium">
              {type ? `No ${typeLabel(type)} items here` : 'This workspace has no items yet'}
            </p>
            <p className="mx-auto mt-1.5 max-w-sm text-[13px] text-dim">
              {type
                ? 'Change the type filter to see other kinds of item.'
                : 'Create a pipeline, SQL script, lakehouse or connection to get started.'}
            </p>
          </div>
        )}

        {data && data.items.length > 0 && (
          <>
            {/* Nợ đã biết: Explorer tải MỘT trang 200 item. Vượt ngưỡng thì danh sách
                hiện thiếu, và điều đó phải nói ra — một danh sách bị cắt âm thầm đọc y
                như một danh sách đầy đủ. */}
            {data.next_cursor && (
              <p
                role="status"
                className="mb-3 rounded-md border border-line bg-warn-soft px-3 py-2 text-[13px] text-warn"
              >
                This workspace has more than 200 items; showing the 200 most recent. Use the type
                filter or ⌘K to find a specific item.
              </p>
            )}

            {current ? (
              <ItemTable
                node={current}
                workspaceId={workspaceId}
                onOpenFolder={(next) => patch((n) => n.set('folder', next), false)}
              />
            ) : (
              // Folder gõ sai trong URL. Nói ra thay vì hiện một bảng rỗng trông y hệt
              // một folder thật đang rỗng.
              <div
                role="alert"
                className="rounded-md border border-line bg-surface p-10 text-center"
              >
                <p className="font-medium">No folder at {folder}</p>
                <button
                  type="button"
                  onClick={() => patch((n) => n.delete('folder'))}
                  className="mt-2 text-[13px] text-accent underline"
                >
                  Back to all items
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {creating && (
        <NewItemDialog
          workspaceId={workspaceId}
          folderPath={folder}
          onClose={() => patch((n) => n.delete('new'))}
        />
      )}
      {managingPerms && (
        <PermissionsDialog
          scopeType="workspaces"
          scopeId={workspaceId}
          onClose={() => patch((n) => n.delete('perms'))}
        />
      )}
    </>
  )
}
