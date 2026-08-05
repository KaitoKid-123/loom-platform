import { useQuery } from '@tanstack/react-query'

import { UnauthorizedError, apiGetWithEtag } from './api'

export interface SearchHit {
  id: string
  workspace_id: string
  type: string
  name: string
  display_name: string
  folder_path: string
}

export function useSearch(term: string) {
  const trimmed = term.trim()

  return useQuery<{ items: SearchHit[] }, Error>({
    queryKey: ['search', trimmed],
    queryFn: async ({ signal }) => {
      // `URLSearchParams` chứ không nối chuỗi: `q=a&b=c` gõ vào ô tìm kiếm sẽ tách
      // thành hai tham số, và server nhận `q=a` — người dùng tìm một thứ, hệ thống tìm
      // thứ khác, không lỗi nào báo.
      const params = new URLSearchParams({ q: trimmed })
      // `signal` do TanStack cấp, và truyền nó xuống fetch là điều DUY NHẤT khiến
      // request cũ bị huỷ khi người dùng gõ tiếp. Không có nó, phản hồi về không đúng
      // thứ tự và danh sách hiện kết quả của một phím trước đó.
      const { data } = await apiGetWithEtag<{ items: SearchHit[] }>(
        `/api/v1/search?${params}`,
        signal,
      )
      return data
    },
    // Query rỗng KHÔNG gọi server: mở palette không nên tốn một round trip cho một
    // câu trả lời mà backend đã cố ý trả rỗng.
    enabled: trimmed.length > 0,
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    staleTime: 10_000,
  })
}
