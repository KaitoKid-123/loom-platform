import { describe, expect, it } from 'vitest'

import { buildLakehouseTree } from './lakehouseSchema'

describe('buildLakehouseTree', () => {
  it('cây hiện đúng bảng vừa được API trả về', () => {
    // Chứng minh đỏ bắt buộc của spec Task 3: một hàm dựng cây trả rỗng vô điều kiện
    // phải làm bài này ĐỎ, không phải chỉ đơn giản "không ném lỗi".
    const tree = buildLakehouseTree({
      namespaces: [
        {
          name: 'sales',
          tables: [{ name: 'orders_2026', columns: null }],
        },
      ],
    })

    const sales = tree.find((ns) => ns.name === 'sales')
    expect(sales?.tables.map((t) => t.name)).toContain('orders_2026')
  })

  it('sắp namespace và bảng theo tên, không theo thứ tự catalog trả về', () => {
    // PyIceberg `list_namespaces`/`list_tables` không đảm bảo thứ tự alphabet — sắp lại
    // ở đây để một bảng mới không "nhảy" vào giữa danh sách tuỳ ý catalog.
    const tree = buildLakehouseTree({
      namespaces: [
        { name: 'zebra', tables: [] },
        { name: 'alpha', tables: [{ name: 'zeta', columns: null }, { name: 'alpha', columns: null }] },
      ],
    })

    expect(tree.map((ns) => ns.name)).toEqual(['alpha', 'zebra'])
    expect(tree[0].tables.map((t) => t.name)).toEqual(['alpha', 'zeta'])
  })

  it('giữ nguyên cột khi depth=columns đã trả về, giữ null khi depth=tables', () => {
    const tree = buildLakehouseTree({
      namespaces: [
        {
          name: 'sales',
          tables: [
            { name: 'orders', columns: [{ name: 'id', type: 'int64' }] },
            { name: 'customers', columns: null },
          ],
        },
      ],
    })

    const tables = tree[0].tables
    expect(tables.find((t) => t.name === 'orders')?.columns).toEqual([
      { name: 'id', type: 'int64' },
    ])
    expect(tables.find((t) => t.name === 'customers')?.columns).toBeNull()
  })

  it('cây rỗng khi lakehouse không có namespace nào', () => {
    expect(buildLakehouseTree({ namespaces: [] })).toEqual([])
  })

  it('phản hồi thiếu mảng namespaces KHÔNG ném — trả cây rỗng', () => {
    // Cùng kỷ luật phòng vệ đã áp cho `folderTree`/`WorkspacePane`: một phản hồi hỏng
    // không được làm nổ cả panel Explorer.
    expect(buildLakehouseTree({} as never)).toEqual([])
  })
})
