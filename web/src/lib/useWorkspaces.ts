import { useQuery } from '@tanstack/react-query'

import { UnauthorizedError, apiGet } from './api'

export interface Workspace {
  id: string
  name: string
  display_name: string
  description: string | null
  domain_id: string | null
  /** Vai trò của CHÍNH người gọi, không phải vai trò cao nhất có trong workspace. */
  my_role: string
}

/**
 * Thứ tự vai trò, từ thấp lên cao. PHẢI khớp `Role` trong
 * `packages/core/src/loom_core/roles.py`.
 *
 * Đây là một bản sao, và bản sao thì trôi. `services/api/tests/test_role_order_sync.py`
 * đọc CHÍNH file này và bác build nếu hai bên lệch — nên "phải khớp" ở trên là một
 * ràng buộc kiểm được, không phải một lời nhắc.
 */
const ROLE_ORDER = ['viewer', 'contributor', 'member', 'admin'] as const

export type RoleName = (typeof ROLE_ORDER)[number]

/**
 * Vai trò `role` có đạt tối thiểu `min` không.
 *
 * Vai trò KHÔNG nhận ra trả `false`. Mặc định an toàn quan trọng ở đây: nếu backend
 * thêm một vai trò mà frontend chưa biết, ẩn nút là sai-nhưng-vô-hại, còn hiện nút
 * là người dùng bấm rồi ăn 403 mà không hiểu vì sao.
 */
export function atLeast(role: string, min: RoleName): boolean {
  const have = ROLE_ORDER.indexOf(role as RoleName)
  return have >= 0 && have >= ROLE_ORDER.indexOf(min)
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export function useWorkspaces() {
  return useQuery<Page<Workspace>, Error>({
    queryKey: ['workspaces'],
    queryFn: () => apiGet('/api/v1/workspaces'),
    // Cùng khuôn với `useCurrentUser`, và `failureCount` là BẮT BUỘC: TanStack coi
    // `true` là "thử lại nữa" chứ không phải "thử lại một lần", nên một predicate bỏ
    // qua đếm lần sẽ retry vô hạn. Giai đoạn 0 đã dính đúng lỗi này.
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    staleTime: 30_000,
  })
}
