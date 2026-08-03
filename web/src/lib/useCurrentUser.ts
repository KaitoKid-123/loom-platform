import { useQuery } from '@tanstack/react-query'

import type { CurrentUser } from '../components/AppShell'
import { UnauthorizedError, apiGet } from './api'

export function useCurrentUser() {
  return useQuery<CurrentUser, Error>({
    queryKey: ['me'],
    queryFn: () => apiGet<CurrentUser>('/api/v1/me'),
    // 401 KHÔNG BAO GIỜ thử lại: nguyên nhân (chưa đăng nhập) không tự khỏi khi
    // gọi lại, và App.tsx cần thấy lỗi ngay để quyết định chuyển hướng. Đặt
    // `retry` ở đây chứ không ở QueryClient để bảo đảm điều đó bất kể người gọi
    // cấu hình QueryClient thế nào.
    //
    // Lỗi khác thì thử lại tối đa 2 lần. PHẢI dùng `failureCount`: TanStack coi
    // giá trị trả về `true` là "thử lại nữa", không phải "thử lại một lần", nên
    // một predicate bỏ qua đếm lần sẽ retry VÔ HẠN — đã kiểm chứng bằng test 15
    // giây không bao giờ thấy lỗi nổi lên.
    retry: (failureCount, error) =>
      !(error instanceof UnauthorizedError) && failureCount < 2,
    staleTime: 5 * 60 * 1000,
  })
}

export { UnauthorizedError }
