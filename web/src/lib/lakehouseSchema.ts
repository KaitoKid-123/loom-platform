/**
 * Hình dạng của `GET /api/v1/lakehouses/{id}/schema` — khớp `LakehouseSchemaOut` bên
 * `loom_query.schemas`. `columns` là `undefined`/`null` khi `?depth=tables` (mặc định):
 * CHƯA đọc cột, không phải "bảng có 0 cột" — xem docstring `loom_query.lakehouse_schema`
 * cho số đo (7,2ms/bảng, co giãn TUYẾN TÍNH theo số bảng) đứng sau quyết định tách depth.
 */
export interface ColumnOut {
  name: string
  type: string
}

export interface TableOut {
  name: string
  columns: ColumnOut[] | null
}

export interface NamespaceOut {
  name: string
  tables: TableOut[]
}

export interface LakehouseSchemaResponse {
  namespaces: NamespaceOut[]
}

export interface TableNode {
  name: string
  columns: ColumnOut[] | null
}

export interface NamespaceNode {
  name: string
  tables: TableNode[]
}

/**
 * Dựng cây namespace -> bảng -> cột từ phản hồi API, SẮP theo tên.
 *
 * PyIceberg `list_namespaces`/`list_tables` (nguồn của phản hồi này, xem
 * `loom_query.lakehouse_schema.build_schema_tree`) không đảm bảo thứ tự alphabet — sắp
 * lại Ở ĐÂY để một bảng vừa tạo không "nhảy" vào giữa danh sách tuỳ theo catalog trả về
 * thứ tự nào, và người dùng luôn tìm thấy nó ở đúng vị trí theo tên, giống cách
 * `folderTree.buildTree` sắp folder/item.
 *
 * `?? []` ở cả hai cấp, cùng kỷ luật đã áp cho `WorkspacePane`/`ItemPage`: một phản hồi
 * thiếu `namespaces` hoặc `tables` không được phép ném — Explorer nằm trong panel trái
 * của vỏ ứng dụng, và một `.map` trần ở đây sẽ thay CẢ MÀN HÌNH bằng trang lỗi của React
 * Router, không chỉ mỗi panel.
 */
export function buildLakehouseTree(schema: LakehouseSchemaResponse): NamespaceNode[] {
  const namespaces = schema?.namespaces ?? []
  return namespaces
    .map((ns) => ({
      name: ns.name,
      tables: (ns.tables ?? [])
        .map((t) => ({ name: t.name, columns: t.columns ?? null }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
}
