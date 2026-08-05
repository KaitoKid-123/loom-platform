import { useParams, useSearchParams } from 'react-router'

import { ExplorerTree } from '../components/Explorer/ExplorerTree'
import { NewItemDialog } from '../components/Explorer/NewItemDialog'
import { buildTree } from '../lib/folderTree'
import { useItems } from '../lib/useItems'

// Đúng bốn loại của backend (`ItemType` trong `item_definitions.py`). Thêm một loại
// không tồn tại vào đây thì bộ lọc gửi `?type=` lạ và ăn 422 — backend liệt kê các loại
// hợp lệ trong thân phản hồi từ cửa chặn 1b, nhưng vẫn là một lỗi vô ích.
const TYPES = ['lakehouse', 'connection', 'pipeline', 'sql_script'] as const

export function ExplorerPage() {
  const { workspaceId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const type = searchParams.get('type') ?? undefined
  // Hộp thoại mở QUA URL: `?new=1` deep-link và sống qua F5 (spec mục 7.4). State
  // React sẽ mất khi tải lại, và một hộp thoại đã điền nửa mà biến mất là mất việc.
  const creating = searchParams.get('new') === '1'

  const closeDialog = () => {
    const next = new URLSearchParams(searchParams)
    next.delete('new')
    setSearchParams(next, { replace: true })
  }

  const { data, isPending, error } = useItems(workspaceId, type)

  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <h1 className="text-lg font-medium">Item</h1>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => {
            const next = new URLSearchParams(searchParams)
            next.set('new', '1')
            setSearchParams(next)
          }}
          className="rounded border border-line px-3 py-1 text-sm hover:bg-muted"
        >
          Tạo item
        </button>
        <label className="text-sm text-dim" htmlFor="type-filter">
          Loại
        </label>
        <select
          id="type-filter"
          value={type ?? ''}
          // Bộ lọc vào QUERY STRING, không vào state React: người dùng gửi được đường
          // dẫn và F5 không mất chỗ đang đứng. Quy tắc bắt buộc của spec mục 7.4.
          //
          // `replace: true` để đổi bộ lọc năm lần không nhồi năm mục vào history — nút
          // Back phải đưa người dùng về chỗ họ ĐẾN TỪ, không lùi qua từng lần lọc.
          onChange={(e) => {
            const next = new URLSearchParams(searchParams)
            if (e.target.value) next.set('type', e.target.value)
            else next.delete('type')
            setSearchParams(next, { replace: true })
          }}
          className="rounded border border-line bg-surface px-2 py-1 text-sm"
        >
          <option value="">Tất cả</option>
          {TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {isPending && (
        <div data-testid="items-skeleton" className="space-y-2">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-7 animate-pulse rounded bg-muted" />
          ))}
        </div>
      )}

      {error && (
        <div role="alert" className="rounded-lg border border-line p-6">
          <p className="font-medium">Không tải được item</p>
          <p className="mt-1 text-sm text-dim">{error.message}</p>
        </div>
      )}

      {data && data.items.length === 0 && (
        <div className="rounded-lg border border-dashed border-line p-8 text-center">
          {/* Phân biệt "workspace rỗng" với "bộ lọc không khớp gì". Gộp hai câu làm
              người dùng vừa đặt bộ lọc tưởng workspace của mình trống. */}
          <p className="text-sm">
            {type ? `Không có item nào thuộc loại ${type}.` : 'Workspace này chưa có item nào.'}
          </p>
          <p className="mt-2 text-sm text-dim">
            {type
              ? 'Đổi bộ lọc để xem những loại khác.'
              : 'Bấm “Tạo item” để bắt đầu — pipeline, SQL script, lakehouse hoặc connection.'}
          </p>
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          {/* Nợ đã biết: Explorer tải MỘT trang 200 item. Vượt ngưỡng thì cây hiện
              thiếu, và điều đó phải nói ra — một cây bị cắt âm thầm đọc y như một cây
              đầy đủ, nên người dùng đi tìm item của mình mà không hiểu vì sao mất. */}
          {data.next_cursor && (
            <p role="status" className="mb-2 rounded border border-line bg-muted px-3 py-2 text-sm">
              Workspace có hơn 200 item; cây đang hiện 200 cái mới nhất. Dùng bộ lọc loại
              hoặc ⌘K để tìm item cụ thể.
            </p>
          )}
          <ExplorerTree node={buildTree(data.items)} workspaceId={workspaceId} />
        </>
      )}

      {creating && <NewItemDialog workspaceId={workspaceId} onClose={closeDialog} />}
    </div>
  )
}
