import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { UnauthorizedError, apiGet, apiGetWithEtag, apiPostJson } from './api'

export interface ItemDetail {
  id: string
  workspace_id: string
  type: string
  name: string
  display_name: string
  folder_path: string
  description: string | null
  definition: Record<string, unknown>
  version: number
  updated_at: string
}

export interface VersionRow {
  version: number
  display_name: string
  folder_path: string
  description: string | null
  change_note: string | null
  created_at: string
  created_by: string
}

// Xuất ra (không còn `function` riêng-file): `useUpdateItemDefinition`
// (`useItemMutations.ts`, Giai đoạn 2c Phần B) cần ghi ĐÚNG hai khoá này sau khi lưu
// một câu SQL — dùng lại nguyên vẹn thay vì chép tay mảng khoá ở nơi khác, đúng lý do
// `itemKeys` bên `useItemMutations.ts` đã tồn tại cho khoá danh sách `['items', ...]`.
export function itemKey(itemId: string) {
  return ['item', itemId] as const
}

export function versionsKey(itemId: string) {
  return ['item-versions', itemId] as const
}

export function useItem(itemId: string) {
  return useQuery<{ data: ItemDetail; etag: string | null }, Error>({
    queryKey: itemKey(itemId),
    // Lấy cả ETag: đó là thứ mọi lần sửa cần gửi lại trong `If-Match`, và ETag đi cùng
    // dữ liệu trong một lời gọi thì không có cửa sổ nào để hai thứ lệch nhau.
    queryFn: () => apiGetWithEtag<ItemDetail>(`/api/v1/items/${itemId}`),
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    enabled: itemId !== '',
  })
}

export function useItemVersions(itemId: string) {
  return useQuery<{ items: VersionRow[]; next_cursor: string | null }, Error>({
    queryKey: versionsKey(itemId),
    queryFn: () => apiGet(`/api/v1/items/${itemId}/versions?limit=50`),
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    enabled: itemId !== '',
  })
}

export interface VersionDetail extends VersionRow {
  definition: Record<string, unknown>
}

/**
 * Nội dung đầy đủ của MỘT version, kể cả `definition`.
 *
 * `enabled` theo `version`: chỉ gọi khi người dùng thật sự mở một version ra xem. Tải
 * sẵn nội dung của mọi version là kéo về cả lịch sử — với item `connection` thì đó là
 * kéo về mọi `secret_ref` từng có.
 */
export function useVersion(itemId: string, version: number | null) {
  return useQuery<VersionDetail, Error>({
    queryKey: ['item-version', itemId, version],
    queryFn: () => apiGet(`/api/v1/items/${itemId}/versions/${version}`),
    enabled: itemId !== '' && version !== null,
  })
}

export function useRestoreVersion(itemId: string, workspaceId: string) {
  const qc = useQueryClient()
  return useMutation<ItemDetail, Error, number>({
    mutationFn: (version) =>
      apiPostJson<ItemDetail>(`/api/v1/items/${itemId}/versions/${version}/restore`, {}),
    onSuccess: () => {
      // Cả BA nơi: item, lịch sử version, và cây Explorer. Bỏ sót cái nào thì người dùng
      // thấy `restore` không có tác dụng cho tới khi họ tải lại trang.
      void qc.invalidateQueries({ queryKey: itemKey(itemId) })
      void qc.invalidateQueries({ queryKey: versionsKey(itemId) })
      void qc.invalidateQueries({ queryKey: ['items', workspaceId] })
    },
  })
}
