import { describe, expect, it } from 'vitest'

import { buildSqlCompletions } from './sqlCompletions'

describe('buildSqlCompletions', () => {
  it('gợi ý bảng và cột từ ĐÚNG schema đưa vào — chứng minh đỏ 6 của Phần C', () => {
    // Một nguồn trả rỗng phải cho gợi ý rỗng: nếu cài đặt lỡ trộn một danh sách cứng
    // vào, bài này vẫn xanh dù nguồn dữ liệu không có gì — đó chính là cái phải bị bắt.
    const suggestions = buildSqlCompletions({
      namespaces: [
        {
          name: 'sales',
          tables: [
            {
              name: 'orders',
              columns: [
                { name: 'id', type: 'int64' },
                { name: 'total', type: 'decimal' },
              ],
            },
          ],
        },
      ],
    })

    expect(suggestions).toContainEqual({
      label: 'sales.orders',
      insertText: 'sales.orders',
      kind: 'table',
    })
    expect(suggestions).toContainEqual({
      label: 'id',
      insertText: 'id',
      kind: 'column',
      detail: 'int64',
    })
    expect(suggestions).toContainEqual({
      label: 'total',
      insertText: 'total',
      kind: 'column',
      detail: 'decimal',
    })
  })

  it('schema rỗng/null cho gợi ý rỗng, không phải danh sách cứng', () => {
    expect(buildSqlCompletions({ namespaces: [] })).toEqual([])
    expect(buildSqlCompletions(null)).toEqual([])
    expect(buildSqlCompletions(undefined)).toEqual([])
  })

  it('khử trùng lặp cột theo tên — nhiều bảng cùng có cột id chỉ gợi ý một lần', () => {
    const suggestions = buildSqlCompletions({
      namespaces: [
        {
          name: 'sales',
          tables: [
            { name: 'orders', columns: [{ name: 'id', type: 'int64' }] },
            { name: 'customers', columns: [{ name: 'id', type: 'int64' }] },
          ],
        },
      ],
    })
    expect(suggestions.filter((s) => s.label === 'id')).toHaveLength(1)
  })

  it('bảng chưa có cột (depth=tables, columns=null) vẫn gợi ý được TÊN BẢNG', () => {
    const suggestions = buildSqlCompletions({
      namespaces: [{ name: 'sales', tables: [{ name: 'orders', columns: null }] }],
    })
    expect(suggestions).toEqual([{ label: 'sales.orders', insertText: 'sales.orders', kind: 'table' }])
  })
})
