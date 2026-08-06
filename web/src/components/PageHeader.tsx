import type { ReactNode } from 'react'
import { Link } from 'react-router'

export interface Crumb {
  label: string
  to?: string
}

/**
 * Breadcrumb + tiêu đề + thanh công cụ, dùng chung cho mọi trang nội dung.
 *
 * Ở một chỗ chứ không lặp trên từng trang: khoảng cách và chiều cao thanh công cụ phải
 * bằng nhau tuyệt đối giữa các trang, nếu không nội dung nhảy lên xuống mỗi lần điều
 * hướng và cảm giác là "ứng dụng bị giật" chứ không ai chỉ ra được vì sao.
 */
export function PageHeader({
  crumbs,
  title,
  actions,
}: {
  crumbs: Crumb[]
  title: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="sticky top-0 z-10 border-b border-line-strong bg-surface">
      <nav aria-label="Breadcrumb" className="px-5 pt-2.5">
        <ol className="flex flex-wrap items-center gap-1 text-[12px] text-dim">
          {crumbs.map((crumb, index) => (
            <li key={`${crumb.label}-${index}`} className="flex items-center gap-1">
              {index > 0 && (
                <span aria-hidden className="text-faint">
                  ›
                </span>
              )}
              {crumb.to ? (
                <Link to={crumb.to} className="rounded px-0.5 hover:text-accent hover:underline">
                  {crumb.label}
                </Link>
              ) : (
                // Mục cuối KHÔNG phải liên kết: một liên kết trỏ về chính trang đang mở
                // là thứ người dùng bấm rồi tưởng nó hỏng vì không có gì xảy ra.
                <span aria-current="page" className="px-0.5 text-ink">
                  {crumb.label}
                </span>
              )}
            </li>
          ))}
        </ol>
      </nav>

      <div className="flex min-h-[46px] flex-wrap items-center gap-3 px-5 py-2">
        <h1 className="text-[17px] font-semibold tracking-tight">{title}</h1>
        <div className="flex-1" />
        {actions}
      </div>
    </div>
  )
}

const VARIANTS = {
  primary: 'bg-accent text-white hover:bg-accent-hover border-accent',
  default: 'bg-surface text-ink hover:bg-hover border-line-strong',
  quiet: 'border-transparent text-dim hover:bg-hover hover:text-ink',
} as const

/**
 * Nút thanh công cụ.
 *
 * Có `primary` vì một thanh công cụ mà mọi nút trông giống nhau thì không có nút nào
 * nổi lên là việc chính — người dùng phải đọc hết mới biết bấm gì. Đúng MỘT nút primary
 * trên mỗi thanh.
 */
export function ToolbarButton({
  variant = 'default',
  className = '',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof VARIANTS }) {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex h-7 items-center gap-1.5 rounded border px-2.5 text-[13px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${VARIANTS[variant]} ${className}`}
    />
  )
}
