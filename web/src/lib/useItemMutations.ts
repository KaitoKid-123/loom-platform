import { type QueryKey, useMutation, useQueryClient } from '@tanstack/react-query'

import { ConflictError, PreconditionRequiredError, apiDelete, apiPatch, apiPostJson } from './api'
import type { TreeItem } from './folderTree'
import { ProblemError } from './problem'
import type { ItemPage } from './useItems'

export interface RenameArgs {
  itemId: string
  etag: string
  displayName: string
}

export interface CreateArgs {
  type: string
  name: string
  display_name: string
  folder_path?: string
  definition: Record<string, unknown>
}

/**
 * Mọi queryKey item của một workspace, BẤT KỂ bộ lọc `type`.
 *
 * Tiền tố chứ không khoá đầy đủ: `useItems` đưa `type` vào key, nên cache có nhiều
 * mục cho cùng một workspace. Ghi lạc quan vào đúng một mục sẽ để những mục khác giữ
 * tên cũ, và người dùng đổi bộ lọc là thấy tên cũ quay lại.
 */
function itemKeys(workspaceId: string): { queryKey: QueryKey } {
  return { queryKey: ['items', workspaceId] }
}

interface RenameContext {
  snapshots: [QueryKey, ItemPage | undefined][]
}

export function useRenameItem(workspaceId: string) {
  const qc = useQueryClient()

  return useMutation<{ data: TreeItem }, Error, RenameArgs, RenameContext>({
    mutationFn: ({ itemId, etag, displayName }) =>
      apiPatch<TreeItem>(`/api/v1/items/${itemId}`, { display_name: displayName }, etag),

    onMutate: async ({ itemId, displayName }) => {
      // Huỷ refetch ĐANG BAY trước khi ghi lạc quan. Một refetch về SAU sẽ ghi đè giá
      // trị mới bằng giá trị cũ, và người dùng thấy tên mình vừa sửa hiện ra rồi tự
      // hoàn tác — không lỗi, không thông báo. Đây là lỗi khó thấy nhất của task này.
      await qc.cancelQueries(itemKeys(workspaceId))

      const snapshots = qc.getQueriesData<ItemPage>(itemKeys(workspaceId))
      for (const [key, page] of snapshots) {
        if (!page) continue
        qc.setQueryData<ItemPage>(key, {
          ...page,
          items: page.items.map((i) =>
            i.id === itemId ? { ...i, display_name: displayName } : i,
          ),
        })
      }
      return { snapshots }
    },

    onError: (_error, _args, context) => {
      // Rollback với MỌI lỗi, không riêng 412: lỗi mạng cũng để cache lệch với server,
      // và một cache lệch âm thầm tệ hơn một lỗi hiện rõ.
      for (const [key, page] of context?.snapshots ?? []) {
        qc.setQueryData(key, page)
      }
    },

    onSettled: () => {
      void qc.invalidateQueries(itemKeys(workspaceId))
    },
  })
}

export function useCreateItem(workspaceId: string) {
  const qc = useQueryClient()
  return useMutation<TreeItem, Error, CreateArgs>({
    mutationFn: (body) => apiPostJson<TreeItem>(`/api/v1/workspaces/${workspaceId}/items`, body),
    // KHÔNG ghi lạc quan khi tạo: server sinh `id`, `version` và `folder_path` đã chuẩn
    // hoá, nên một hàng giả trong cache sẽ mang id sai và mọi liên kết tới nó vỡ.
    onSuccess: () => {
      void qc.invalidateQueries(itemKeys(workspaceId))
    },
  })
}

export function useDeleteItem(workspaceId: string) {
  const qc = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (itemId) => apiDelete(`/api/v1/items/${itemId}`),
    onSuccess: () => {
      void qc.invalidateQueries(itemKeys(workspaceId))
    },
  })
}

/**
 * Câu hiện cho người dùng khi một mutation hỏng.
 *
 * Giữ NGUYÊN thông báo của server rồi thêm bước tiếp theo. Với 412 thì thông báo đó
 * nói bản hiện tại là mấy — thay nó bằng "Có lỗi" là bỏ đi thông tin duy nhất giải
 * thích vì sao thứ họ vừa gõ không được lưu.
 */
export function describeError(error: Error): string {
  if (error instanceof ConflictError) {
    return `${error.message}. Tải lại để xem bản mới nhất rồi sửa lại.`
  }
  if (error instanceof PreconditionRequiredError) {
    // Lỗi của client, không của người dùng: ta gửi PATCH mà không có ETag. Nói thật
    // thay vì bịa một câu đổ cho người dùng.
    return `${error.message}. Tải lại trang rồi thử lại.`
  }
  if (error instanceof ProblemError && Object.keys(error.fieldErrors).length > 0) {
    return Object.entries(error.fieldErrors)
      .map(([field, message]) => `${field}: ${message}`)
      .join('; ')
  }
  return error.message
}
