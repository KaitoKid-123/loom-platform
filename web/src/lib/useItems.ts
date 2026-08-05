import { useQuery } from '@tanstack/react-query'

import { UnauthorizedError, apiGet } from './api'
import type { TreeItem } from './folderTree'

export interface ItemPage {
  items: TreeItem[]
  next_cursor: string | null
}

/**
 * Trần của backend là 200 (`_MAX_LIMIT` trong `routers/items.py`), nên xin đúng 200.
 *
 * NỢ ĐÃ BIẾT: Giai đoạn 1 tải MỘT trang và không phân trang trong Explorer — cây cần
 * cả tập để dựng được nhánh. Workspace vượt 200 item sẽ hiện thiếu, nên trang phải nói
 * ra khi `next_cursor` khác null. Một cây bị cắt âm thầm đọc y như một cây đầy đủ.
 */
const PAGE_LIMIT = 200

export function useItems(workspaceId: string, type?: string) {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) })
  if (type) params.set('type', type)

  return useQuery<ItemPage, Error>({
    // `type` nằm TRONG queryKey. Thiếu nó thì đổi bộ lọc vẫn trả cache của bộ lọc
    // trước, và người dùng thấy danh sách sai một nhịp rồi mới đúng — trông như dữ
    // liệu tự nhảy.
    queryKey: ['items', workspaceId, type ?? null],
    queryFn: () => apiGet(`/api/v1/workspaces/${workspaceId}/items?${params}`),
    // Cùng khuôn `useCurrentUser`: `failureCount` là bắt buộc, xem `useWorkspaces`.
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    enabled: workspaceId !== '',
  })
}
