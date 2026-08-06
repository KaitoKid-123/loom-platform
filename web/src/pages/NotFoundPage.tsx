import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <div className="mx-auto mt-16 max-w-lg rounded-md border border-dashed border-line-strong bg-surface p-10 text-center">
      <h1 className="text-[15px] font-semibold">Page not found</h1>
      {/* Câu thứ hai có chủ đích. Backend trả 404 cho tài nguyên người gọi không
          được đọc (spec mục 4.5), nên một item vừa mất quyền cũng ra đúng trang này.
          Nói riêng "trang không tồn tại" sẽ khiến người dùng tưởng dữ liệu bị xoá và
          đi tìm bản backup. */}
      <p className="mt-2 text-[13px] leading-relaxed text-dim">
        This address does not exist. It may have moved, or you may no longer have
        permission to see it.
      </p>
      <Link to="/" className="mt-4 inline-block text-[13px] text-accent underline">
        Back to workspaces
      </Link>
    </div>
  )
}
