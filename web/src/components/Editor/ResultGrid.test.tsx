import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ResultGrid } from './ResultGrid'

const COLUMNS = [
  { name: 'id', type: 'int64' },
  { name: 'total', type: 'decimal' },
]

describe('ResultGrid', () => {
  it('hiện cột và dòng', () => {
    render(<ResultGrid columns={COLUMNS} rows={[[1, 9.5]]} truncated={false} rowCount={1} />)
    expect(screen.getByText('id')).toBeInTheDocument()
    expect(screen.getByText('total')).toBeInTheDocument()
    expect(screen.getByText('9.5')).toBeInTheDocument()
  })

  it('NULL hiện thành nhãn riêng, không phải ô trống gây hiểu nhầm', () => {
    render(<ResultGrid columns={COLUMNS} rows={[[null, 1]]} truncated={false} rowCount={1} />)
    expect(screen.getByText('NULL')).toBeInTheDocument()
  })

  it('truncated=true PHẢI hiện cờ cắt bớt — chứng minh đỏ 2 của Phần A', () => {
    // Bỏ cờ `truncated` khỏi giao diện thì 10.000 dòng đầu trông y hệt toàn bộ kết
    // quả — một component bỏ qua props này (chỉ vẽ bảng, không đọc `truncated`) phải
    // làm bài này ĐỎ.
    render(<ResultGrid columns={COLUMNS} rows={[[1, 2]]} truncated rowCount={50000} />)
    expect(screen.getByRole('status')).toHaveTextContent(/truncated/i)
    expect(screen.getByRole('status')).toHaveTextContent('50,000')
  })

  it('truncated=false KHÔNG hiện cờ cắt bớt', () => {
    render(<ResultGrid columns={COLUMNS} rows={[[1, 2]]} truncated={false} rowCount={1} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('không có cột nào vẫn không ném — hiện thông báo thay vì bảng rỗng gây hiểu nhầm', () => {
    render(<ResultGrid columns={[]} rows={[]} truncated={false} rowCount={0} />)
    expect(screen.getByRole('status')).toHaveTextContent(/no columns/i)
  })
})
