/**
 * Icon theo loại item.
 *
 * SVG chứ không emoji. Emoji render khác nhau trên từng hệ điều hành — 🏛 là một toà
 * nhà xám trên Windows và một đền thờ màu be trên macOS — nên bảng item trông khác nhau
 * với từng người, và không có cách nào chỉnh màu để khớp giao diện.
 *
 * Mỗi loại một MÀU riêng, giống Fabric. Trong một bảng ba mươi dòng, màu là thứ mắt bắt
 * trước cả chữ, nên nó là cách nhanh nhất để thấy "cái nào là pipeline".
 */

export type KnownItemType = 'lakehouse' | 'pipeline' | 'sql_script' | 'connection'

const PATHS: Record<KnownItemType, { color: string; label: string; d: string }> = {
  // Nhà kho: mái và các cột — kho dữ liệu.
  lakehouse: {
    color: 'var(--color-type-lakehouse)',
    label: 'Lakehouse',
    d: 'M2 6.5 8 3l6 3.5M3.5 7v5.5M6.2 7v5.5M9.8 7v5.5M12.5 7v5.5M2.5 13h11',
  },
  // Ba nút nối bằng hai cạnh — luồng có nhánh.
  pipeline: {
    color: 'var(--color-type-pipeline)',
    label: 'Pipeline',
    d: 'M4 4.5h1.5M4 11.5h1.5M10.5 8H12M5.5 4.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0ZM5.5 11.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0ZM13.5 8a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0ZM5.5 4.5h2.5a1 1 0 0 1 1 1V8M5.5 11.5h2.5a1 1 0 0 0 1-1V8',
  },
  // Trang có dấu nhắc lệnh — script.
  sql_script: {
    color: 'var(--color-type-sql)',
    label: 'SQL script',
    d: 'M3.5 2.5h6L12.5 5.5v8a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1ZM9.5 2.5v3h3M5 9.5l1.5 1.5L5 12.5M8 12.5h2.5',
  },
  // Phích cắm: hai chân, thân, và dây. Bản trước dùng hai cung tròn và render ra một
  // nét chéo không đọc được ở 16px — đã thấy trên ảnh chụp thật.
  connection: {
    color: 'var(--color-type-connection)',
    label: 'Connection',
    d: 'M6 2v3.2M10 2v3.2M4.4 5.2h7.2v2.6a3.6 3.6 0 0 1-7.2 0V5.2ZM8 11.4V14',
  },
}

const FALLBACK = {
  color: 'var(--color-faint)',
  label: 'Item',
  d: 'M3.5 2.5h6L12.5 5.5v8a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1ZM9.5 2.5v3h3',
}

export function typeLabel(type: string): string {
  return PATHS[type as KnownItemType]?.label ?? type
}

interface Props {
  type: string
  size?: number
  className?: string
}

export function ItemTypeIcon({ type, size = 16, className }: Props) {
  const glyph = PATHS[type as KnownItemType] ?? FALLBACK
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke={glyph.color}
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      // `aria-hidden` vì tên loại LUÔN đi kèm dưới dạng chữ trong cột "Type" hoặc nhãn
      // bên cạnh. Đọc nó hai lần chỉ làm screen reader dài dòng.
      aria-hidden
      className={className}
    >
      <path d={glyph.d} />
    </svg>
  )
}

/** Icon folder, cùng ngôn ngữ nét với icon loại item. */
export function FolderIcon({ size = 16, open = false }: { size?: number; open?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="var(--color-warn)"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {open ? (
        <path d="M1.8 12.5V4a1 1 0 0 1 1-1h3.3l1.4 1.6h4.7a1 1 0 0 1 1 1v1.4M1.8 12.5l1.7-5h11l-1.7 5h-11Z" />
      ) : (
        <path d="M1.8 4.5a1 1 0 0 1 1-1h3.3l1.4 1.6h5.7a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1h-10.4a1 1 0 0 1-1-1v-8Z" />
      )}
    </svg>
  )
}
