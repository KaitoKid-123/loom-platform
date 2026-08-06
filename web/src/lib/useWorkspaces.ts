import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { UnauthorizedError, apiGet, apiPatch, apiPostJson } from './api'

export interface Workspace {
  id: string
  name: string
  display_name: string
  description: string | null
  domain_id: string | null
  /** ETag của workspace — client cần nó cho `If-Match` khi sửa. */
  version: number
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

export interface WorkspaceList extends Page<Workspace> {
  /** Vai trò của người gọi ở cấp tenant, hoặc `null`. Chỉ admin mới tạo được workspace. */
  tenant_role: string | null
}

export function useWorkspaces() {
  return useQuery<WorkspaceList, Error>({
    queryKey: ['workspaces'],
    queryFn: () => apiGet('/api/v1/workspaces'),
    // Cùng khuôn với `useCurrentUser`, và `failureCount` là BẮT BUỘC: TanStack coi
    // `true` là "thử lại nữa" chứ không phải "thử lại một lần", nên một predicate bỏ
    // qua đếm lần sẽ retry vô hạn. Giai đoạn 0 đã dính đúng lỗi này.
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    staleTime: 30_000,
  })
}

export interface CreateWorkspaceArgs {
  name: string
  display_name: string
  description?: string
  domain_id?: string
}

export function useCreateWorkspace() {
  const qc = useQueryClient()
  return useMutation<Workspace, Error, CreateWorkspaceArgs>({
    mutationFn: (body) => apiPostJson<Workspace>('/api/v1/workspaces', body),
    onSuccess: () => {
      // Cả hai: danh sách workspace, và danh sách domain — `workspace_count` của domain
      // vừa đổi, và một con số cũ trông y như một con số đúng.
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      void qc.invalidateQueries({ queryKey: ['domains'] })
    },
  })
}

export interface PatchWorkspaceArgs {
  id: string
  /** ETag hiện tại. Thiếu nó server trả 428, và đó là lỗi của client chứ không người dùng. */
  etag: string
  display_name?: string
  description?: string
  domain_id?: string
  clear_domain?: boolean
}

export function usePatchWorkspace() {
  const qc = useQueryClient()
  return useMutation<Workspace, Error, PatchWorkspaceArgs>({
    mutationFn: async ({ id, etag, ...body }) => {
      const { data } = await apiPatch<Workspace>(`/api/v1/workspaces/${id}`, body, etag)
      return data
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      void qc.invalidateQueries({ queryKey: ['domains'] })
    },
  })
}
