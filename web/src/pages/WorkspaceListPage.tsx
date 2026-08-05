import { Link, useNavigate } from 'react-router'

import { atLeast, useWorkspaces } from '../lib/useWorkspaces'

export function WorkspaceListPage() {
  const { data, isPending, error } = useWorkspaces()
  const navigate = useNavigate()

  if (isPending) {
    // Skeleton theo HÌNH của nội dung sắp tới, không phải spinner toàn trang — quy
    // tắc bắt buộc của spec mục 7.4. Spinner toàn trang làm cả khung nhảy một nhịp
    // rồi nhảy lại, và người dùng mất chỗ mắt đang đặt.
    return (
      <div data-testid="workspace-skeleton" className="space-y-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-16 animate-pulse rounded-lg border border-line bg-muted" />
        ))}
      </div>
    )
  }

  if (error) {
    return (
      <div role="alert" className="rounded-lg border border-line p-6">
        <h1 className="font-medium">Không tải được danh sách workspace</h1>
        {/* Thông báo của server, nguyên văn. Thay bằng "Có lỗi" là bỏ đi thứ duy nhất
            giúp người dùng hoặc người vận hành biết chuyện gì vừa xảy ra. */}
        <p className="mt-1 text-sm text-dim">{error.message}</p>
      </div>
    )
  }

  const items = data?.items ?? []

  return (
    <div>
      <h1 className="mb-4 text-lg font-medium">Workspace</h1>
      {items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-line p-8 text-center">
          <p className="text-sm">Bạn chưa có quyền trên workspace nào.</p>
          {/* Trạng thái rỗng phải nói BƯỚC TIẾP THEO, không chỉ nói là rỗng. Và nhắc
              tới nhóm vì vai trò gán cho nhóm cấp quyền y như gán cho cá nhân
              (Task 25) — người dùng không tự biết điều đó. */}
          <p className="mt-2 text-sm text-dim">
            Nhờ quản trị viên gán vai trò cho bạn, hoặc cho một nhóm bạn thuộc.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((ws) => (
            <li key={ws.id} className="rounded-lg border border-line p-4">
              <div className="flex items-center gap-3">
                <Link to={`/workspaces/${ws.id}/items`} className="font-medium underline">
                  {ws.display_name}
                </Link>
                <span className="rounded bg-muted px-2 py-0.5 text-xs text-dim">{ws.my_role}</span>
                <div className="flex-1" />
                {/* Ẩn nút mà server sẽ từ chối. Chặn ở server là bắt buộc và đã có; ẩn
                    ở đây là để người dùng không bấm rồi ăn 403 mà không hiểu vì sao. */}
                {atLeast(ws.my_role, 'contributor') && (
                  <button
                    type="button"
                    // `?new=1` chứ không state React: hộp thoại tạo item mở được bằng
                    // đường dẫn, nên nó deep-link và F5 được (spec mục 7.4).
                    onClick={() => navigate(`/workspaces/${ws.id}/items?new=1`)}
                    className="rounded border border-line px-3 py-1 text-sm hover:bg-muted"
                  >
                    Tạo item
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
