import { useState } from 'react'

import { describeError, useRenameItem } from '../../lib/useItemMutations'

interface Props {
  workspaceId: string
  itemId: string
  /** ETag của bản đang xem. Không có nó thì server trả 428 và không ai sửa được gì. */
  etag: string
  current: string
  onDone: () => void
}

export function RenameInline({ workspaceId, itemId, etag, current, onDone }: Props) {
  const [value, setValue] = useState(current)
  const rename = useRenameItem(workspaceId)

  const submit = () => {
    const next = value.trim()
    // Không gửi khi rỗng hoặc không đổi: backend coi PATCH không đổi gì là hợp lệ và
    // KHÔNG bump version, nhưng nó vẫn là một round trip và một dòng audit vô nghĩa.
    if (!next || next === current) {
      onDone()
      return
    }
    rename.mutate({ itemId, etag, displayName: next }, { onSuccess: onDone })
  }

  return (
    <div className="flex items-center gap-2">
      <input
        // eslint-disable-next-line jsx-a11y/no-autofocus -- ô này xuất hiện do người
        // dùng vừa chọn "Đổi tên"; không lấy nét thì họ phải bấm thêm một lần nữa.
        autoFocus
        aria-label="Tên hiển thị"
        value={value}
        disabled={rename.isPending}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onDone()
          if (e.key === 'Enter') submit()
        }}
        // `onBlur` KHÔNG lưu. Mất nét là chuyện xảy ra vì nhiều lý do — bấm ra ngoài,
        // đổi tab, một hộp thoại khác mở ra — và lưu trong những lúc đó biến một cú
        // bấm lạc tay thành một lần đổi tên không ai định làm.
        className="rounded border border-line bg-surface px-2 py-1 text-sm disabled:opacity-50"
      />
      {rename.isError && rename.error && (
        // `role="alert"` để screen reader đọc ngay: người dùng vừa gõ xong và cần biết
        // rằng thứ họ gõ KHÔNG được lưu.
        <span role="alert" className="text-sm text-dim">
          {describeError(rename.error)}
        </span>
      )}
    </div>
  )
}
