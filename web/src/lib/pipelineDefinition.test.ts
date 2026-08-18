import { describe, expect, it } from 'vitest'

import { describeStep, diagramNodes, mergeStepStatus, parseSteps } from './pipelineDefinition'
import type { PipelineStepView, StepRunView } from './pipelineDefinition'

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

const INGEST: PipelineStepView = {
  type: 'ingest',
  ingest: {
    lakehouse_id: 'lh-1',
    connection_id: 'conn-1',
    stream: 'public.orders',
    mode: 'incremental',
  },
}
const SQL: PipelineStepView = {
  type: 'sql',
  sql: { lakehouse_id: 'lh-1', sql: 'select 1' },
}

describe('diagramNodes', () => {
  it('một node cho mỗi bước, theo thứ tự, không trạng thái khi không có run', () => {
    const nodes = diagramNodes([INGEST, SQL])
    expect(nodes.map((n) => n.type)).toEqual(['ingest', 'sql'])
    expect(nodes.map((n) => n.status)).toEqual(['none', 'none'])
    expect(nodes[0].index).toBe(0)
    expect(nodes[1].index).toBe(1)
  })

  it('mang trạng thái vào khi có stepRuns', () => {
    const stepRuns: StepRunView[] = [
      { step_index: 0, step_type: 'ingest', status: 'succeeded', error: null },
      { step_index: 1, step_type: 'sql', status: 'failed', error: 'boom' },
    ]
    const nodes = diagramNodes([INGEST, SQL], stepRuns)
    expect(nodes.map((n) => n.status)).toEqual(['succeeded', 'failed'])
  })

  it('mảng bước rỗng cho ra mảng node rỗng', () => {
    expect(diagramNodes([])).toEqual([])
  })
})

describe('mergeStepStatus', () => {
  it('ghép theo step_index, KHÔNG theo vị trí mảng', () => {
    // `step_index` không liên tục: run này chạy trên một definition CŨ có ba bước, và
    // bước ở giữa đã bị xoá khỏi definition hiện tại. Ghép theo VỊ TRÍ sẽ gán trạng
    // thái của step_index 2 cho bước ở vị trí 1 — tức nói sai bước nào đã hỏng, đúng
    // lúc người ta đang tìm hiểu vì sao run hỏng.
    const stepRuns: StepRunView[] = [
      { step_index: 0, step_type: 'ingest', status: 'succeeded', error: null },
      { step_index: 2, step_type: 'sql', status: 'failed', error: 'boom' },
    ]
    const merged = mergeStepStatus([INGEST, SQL, SQL], stepRuns)

    expect(merged.map((m) => m.status)).toEqual(['succeeded', 'none', 'failed'])
    expect(merged[1].run).toBeNull()
    expect(merged[2].run?.error).toBe('boom')
  })

  it('bỏ qua một stepRun trỏ ra ngoài chuỗi bước hiện tại', () => {
    // Run của một definition có NHIỀU bước hơn bản hiện tại. Không được ném, và không
    // được đẻ ra một node thứ ba mà definition không có.
    const stepRuns: StepRunView[] = [
      { step_index: 0, step_type: 'ingest', status: 'succeeded', error: null },
      { step_index: 7, step_type: 'sql', status: 'failed', error: 'boom' },
    ]
    const merged = mergeStepStatus([INGEST], stepRuns)
    expect(merged).toHaveLength(1)
    expect(merged[0].status).toBe('succeeded')
  })

  it('không stepRuns thì mọi bước là none', () => {
    const merged = mergeStepStatus([INGEST, SQL], [])
    expect(merged.map((m) => m.status)).toEqual(['none', 'none'])
  })
})
