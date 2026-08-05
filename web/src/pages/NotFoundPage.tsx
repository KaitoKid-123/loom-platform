import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-lg rounded-lg border border-dashed border-line p-8 text-center">
      <h1 className="text-lg font-medium">Không tìm thấy trang</h1>
      {/* Câu thứ hai có chủ đích. Backend trả 404 cho tài nguyên người gọi không
          được đọc (spec mục 4.5), nên một item vừa mất quyền cũng ra đúng trang này.
          Nói riêng "trang không tồn tại" sẽ khiến người dùng tưởng dữ liệu bị xoá và
          đi tìm bản backup. */}
      <p className="mt-2 text-sm text-dim">
        Đường dẫn này không tồn tại. Có thể nó đã đổi, hoặc bạn không còn quyền xem.
      </p>
      <Link to="/" className="mt-4 inline-block text-sm underline">
        Về danh sách workspace
      </Link>
    </div>
  )
}
