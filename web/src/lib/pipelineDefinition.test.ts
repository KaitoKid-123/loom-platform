import { describe, expect, it } from 'vitest'

import { describeStep, parseSteps } from './pipelineDefinition'

describe('parseSteps', () => {
  it('đọc một chuỗi bước hợp lệ theo đúng thứ tự', () => {
    const steps = parseSteps({
      schema_version: 1,
      steps: [
        {
          type: 'ingest',
          ingest: {
            lakehouse_id: 'lh-1',
            connection_id: 'conn-1',
            stream: 'public.orders',
            mode: 'incremental',
          },
        },
        { type: 'sql', sql: { lakehouse_id: 'lh-1', sql: 'select 1' } },
      ],
    })

    expect(steps).toHaveLength(2)
    expect(steps[0].type).toBe('ingest')
    expect(steps[0].ingest?.stream).toBe('public.orders')
    expect(steps[1].type).toBe('sql')
    expect(steps[1].sql?.sql).toBe('select 1')
  })

  it('trả mảng rỗng cho một pipeline chưa có bước nào', () => {
    expect(parseSteps({ schema_version: 1, steps: [] })).toEqual([])
  })

  // Bốn phép kiểm dưới đây là MỘT phép canh: một `definition` lạ không được phép
  // hạ cả trang xuống trang lỗi của React Router. Đã gặp thật ở Giai đoạn 0 —
  // `name.split` trên `undefined` làm trắng toàn bộ màn hình (xem `initials()` ở
  // `AppShell.tsx`). Một `definition` sửa tay hoặc một migration sau này thêm loại
  // bước đều đi qua đúng đường này.
  it('không ném khi `steps` không phải mảng', () => {
    expect(parseSteps({ steps: 'không phải mảng' })).toEqual([])
  })

  it('không ném khi definition là null hay không phải object', () => {
    expect(parseSteps(null)).toEqual([])
    expect(parseSteps(42)).toEqual([])
    expect(parseSteps(undefined)).toEqual([])
  })

  it('bỏ qua phần tử có `type` lạ thay vì ném', () => {
    const steps = parseSteps({
      steps: [
        { type: 'quantum', whatever: true },
        { type: 'sql', sql: { lakehouse_id: 'lh-1', sql: 'select 1' } },
      ],
    })
    expect(steps).toHaveLength(1)
    expect(steps[0].type).toBe('sql')
  })

  it('bỏ qua bước thiếu khối config khớp `type`', () => {
    // Server từ chối trạng thái này (`PipelineStep._config_matches_type`), nhưng một
    // hàng cũ hoặc sửa tay vẫn có thể mang nó, và giao diện phải đọc được phần còn lại.
    expect(parseSteps({ steps: [{ type: 'ingest' }] })).toEqual([])
    expect(parseSteps({ steps: [{ type: 'sql' }] })).toEqual([])
  })
})

describe('describeStep', () => {
  it('mô tả một bước nạp bằng stream và mode', () => {
    const text = describeStep({
      type: 'ingest',
      ingest: {
        lakehouse_id: 'lh-1',
        connection_id: 'conn-1',
        stream: 'public.orders',
        mode: 'incremental',
      },
    })
    expect(text).toContain('public.orders')
    expect(text).toContain('incremental')
  })

  it('cắt câu SQL dài và KHÔNG cắt câu ngắn', () => {
    const long = describeStep({
      type: 'sql',
      sql: { lakehouse_id: 'lh-1', sql: 'select '.repeat(30) + 'end' },
    })
    expect(long.length).toBeLessThan(90)
    expect(long).toContain('…')

    const short = describeStep({
      type: 'sql',
      sql: { lakehouse_id: 'lh-1', sql: 'select 1' },
    })
    expect(short).toContain('select 1')
    expect(short).not.toContain('…')
  })

  it('gộp mọi khoảng trắng trong SQL về một dấu cách', () => {
    // Không gộp thì một câu SQL nhiều dòng làm hàng danh sách cao gấp năm và bố cục vỡ.
    const text = describeStep({
      type: 'sql',
      sql: { lakehouse_id: 'lh-1', sql: 'select\n  a,\n  b\nfrom t' },
    })
    expect(text).not.toContain('\n')
    expect(text).toContain('select a, b from t')
  })
})
