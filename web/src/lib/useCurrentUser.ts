import { useQuery } from '@tanstack/react-query'

import type { CurrentUser } from '../components/AppShell'
import { UnauthorizedError, apiGet } from './api'

export function useCurrentUser() {
  return useQuery<CurrentUser, Error>({
    queryKey: ['me'],
    queryFn: () => apiGet<CurrentUser>('/api/v1/me'),
    // Không bao giờ thử lại. Quan trọng nhất là 401: nguyên nhân (chưa đăng
    // nhập) không tự khỏi khi gọi lại, và App.tsx cần thấy lỗi này ngay để
    // quyết định chuyển hướng — đặt `retry` ở đây (không phải ở QueryClient)
    // để bảo đảm điều đó bất kể QueryClient của người gọi cấu hình gì.
    // Lỗi khác cũng không thử lại: option `retry` truyền vào useQuery đè lên
    // `defaultOptions` của QueryClient (xác nhận trong query-core), nên nếu
    // hàm này trả `true` cho lỗi khác 401 thì nó thử lại vô hạn — đã tự kiểm
    // chứng bằng test 15s không bao giờ thấy lỗi hiện ra. Thất bại nhanh,
    // hiện lỗi ngay, còn hơn "Đang tải…" treo vô định.
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
}

export { UnauthorizedError }
