import { useQuery } from '@tanstack/react-query'

import { UnauthorizedError, apiGetWithEtag } from './api'
import type { LakehouseSchemaResponse } from './lakehouseSchema'

export type SchemaDepth = 'tables' | 'columns'

/**
 * `GET /api/v1/lakehouses/{id}/schema?depth=`.
 *
 * `depth` nằm TRONG queryKey — cùng lý do `type` nằm trong key của `useItems`: hai độ
 * sâu là hai bộ dữ liệu khác nhau (một bên `columns: null`, một bên có cột thật), gộp
 * chung một khoá sẽ làm React Query trả nhầm cache của depth kia.
 *
 * `enabled` thứ hai (ngoài `lakehouseId !== ''`) là chỗ Explorer trì hoãn `depth=columns`
 * tới khi người dùng thật sự mở một bảng ra — xem `LakehouseSchemaTree`. Số đo thật
 * (docstring `loom_query.lakehouse_schema`): `depth=tables` ~13ms/~4KB cho 200 bảng,
 * `depth=columns` ~1,5s/~221KB CÙNG lakehouse đó — gọi `columns` vô điều kiện là bắt
 * mọi người chờ 1,5 giây cho thứ họ chưa hỏi.
 *
 * Dùng `apiGetWithEtag` (bỏ etag) chứ không `apiGet`: lỗi đi qua `raise()` nên
 * `error.message` mang nguyên văn `detail` của server (vd. câu 403 của `SchemaForbidden`)
 * thay vì chỉ "…trả về 403" — quy tắc bắt buộc spec 7.4 "thông báo lỗi của server,
 * nguyên văn".
 */
export function useLakehouseSchema(lakehouseId: string, depth: SchemaDepth, enabled = true) {
  return useQuery<LakehouseSchemaResponse, Error>({
    queryKey: ['lakehouse-schema', lakehouseId, depth],
    queryFn: () =>
      apiGetWithEtag<LakehouseSchemaResponse>(
        `/api/v1/lakehouses/${lakehouseId}/schema?depth=${depth}`,
      ).then((r) => r.data),
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    enabled: lakehouseId !== '' && enabled,
  })
}
