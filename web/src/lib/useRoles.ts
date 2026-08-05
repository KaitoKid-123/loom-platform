import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { UnauthorizedError, apiDelete, apiGet, apiPut } from './api'

export type ScopeType = 'workspaces' | 'items'

export interface RoleRow {
  principal_type: 'user' | 'group'
  user_id: string | null
  group: string | null
  role: string
}

export interface RolesPayload {
  items: RoleRow[]
  /** Vai trò người gọi ĐƯỢC PHÉP gán — do SERVER tính, không đoán ở client. */
  grantable_roles: string[]
}

/** Số nhiều, khớp đúng route của backend (`/workspaces/{id}/roles`, `/items/{id}/roles`). */
function rolesPath(scopeType: ScopeType, scopeId: string): string {
  return `/api/v1/${scopeType}/${scopeId}/roles`
}

function rolesKey(scopeType: ScopeType, scopeId: string) {
  return ['roles', scopeType, scopeId] as const
}

export function useRoles(scopeType: ScopeType, scopeId: string) {
  return useQuery<RolesPayload, Error>({
    queryKey: rolesKey(scopeType, scopeId),
    queryFn: () => apiGet(rolesPath(scopeType, scopeId)),
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
  })
}

export interface PrincipalRef {
  user_id?: string
  group?: string
}

export function useGrantRole(scopeType: ScopeType, scopeId: string) {
  const qc = useQueryClient()
  return useMutation<void, Error, PrincipalRef & { role: string }>({
    mutationFn: (body) => apiPut(rolesPath(scopeType, scopeId), body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: rolesKey(scopeType, scopeId) })
    },
  })
}

export function useRevokeRole(scopeType: ScopeType, scopeId: string) {
  const qc = useQueryClient()
  return useMutation<void, Error, PrincipalRef>({
    // Principal đi trong QUERY STRING, không trong body: backend cố ý nhận nó ở đó vì
    // RFC 9110 nói client không nên gửi nội dung trong DELETE và một số gateway lược nó
    // đi — một lệnh thu bị mất body là yêu cầu thiếu đúng phần nói THU CỦA AI.
    mutationFn: (principal) => {
      const params = new URLSearchParams(
        principal.user_id ? { user_id: principal.user_id } : { group: principal.group ?? '' },
      )
      return apiDelete(`${rolesPath(scopeType, scopeId)}?${params}`)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: rolesKey(scopeType, scopeId) })
    },
  })
}

/**
 * Số assignment mang vai trò `admin` — tính CẢ NHÓM, giống backend.
 *
 * Chỉ đếm người thì UI vô hiệu nút oan khi còn một nhóm admin, hoặc bật nút trong khi
 * đó thật sự là admin cuối cùng và server sẽ trả 409.
 */
export function countAdmins(rows: RoleRow[]): number {
  return rows.filter((r) => r.role === 'admin').length
}
