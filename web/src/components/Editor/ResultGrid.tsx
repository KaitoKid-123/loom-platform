import type { QueryColumn } from '../../lib/queryApi'

interface Props {
  columns: QueryColumn[]
  rows: unknown[][]
  truncated: boolean
  rowCount: number
}

/**
 * Lưới kết quả — Giai đoạn 2c Phần A.
 *
 * `truncated`/`rowCount` PHẢI hiện ra, không phải trang trí: `loom-query` cắt kết quả ở
 * 10.000 dòng (mặc định, xem `QueryStatusOut` docstring) và `truncated=True` là cách
 * DUY NHẤT client biết `rows` không phải toàn bộ. Bỏ cờ này thì 10.000 dòng đầu trông y
 * hệt toàn bộ kết quả, và một báo cáo dựa trên nó sẽ sai mà không ai biết — chứng minh
 * đỏ 2 của Phần A canh đúng câu này.
 */
export function ResultGrid({ columns, rows, truncated, rowCount }: Props) {
  if (columns.length === 0) {
    return (
      <p role="status" className="text-[13px] text-dim">
        Query succeeded — no columns returned.
      </p>
    )
  }

  return (
    <div>
      {truncated && (
        <p
          role="status"
          className="mb-2 rounded border border-line bg-warn-soft px-2.5 py-1.5 text-[13px] text-warn"
        >
          Showing the first {rows.length.toLocaleString('en-US')} of{' '}
          {rowCount.toLocaleString('en-US')} rows — the result was truncated.
        </p>
      )}
      <div className="overflow-auto rounded-md border border-line">
        <table className="w-full text-left text-[12px]">
          <thead className="bg-raised">
            <tr>
              {columns.map((col) => (
                <th
                  key={col.name}
                  className="border-b border-line-strong px-2 py-1 font-medium text-dim"
                >
                  {col.name}
                  <span className="ml-1.5 font-normal text-faint">{col.type}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b border-line last:border-0">
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="tabular px-2 py-1 font-mono">
                    {cell === null || cell === undefined ? (
                      <span className="text-faint">NULL</span>
                    ) : (
                      String(cell)
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1.5 text-[12px] text-faint">
        {rowCount.toLocaleString('en-US')} row{rowCount === 1 ? '' : 's'}
        {truncated ? ' (truncated)' : ''}
      </p>
    </div>
  )
}
