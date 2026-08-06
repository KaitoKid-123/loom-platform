import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ItemTypeIcon, typeLabel } from './ItemTypeIcon'

describe('typeLabel', () => {
  it('đổi slug kỹ thuật thành nhãn người đọc được', () => {
    expect(typeLabel('sql_script')).toBe('SQL script')
    expect(typeLabel('lakehouse')).toBe('Lakehouse')
  })

  it('loại lạ trả về CHÍNH nó, không phải chuỗi rỗng', () => {
    // Backend thêm một loại mà frontend chưa biết: hiện slug còn hơn hiện một ô trống,
    // vì slug ít ra nói được đó là cái gì.
    expect(typeLabel('notebook')).toBe('notebook')
  })
})

describe('ItemTypeIcon', () => {
  it('mỗi loại một MÀU riêng — màu là thứ mắt bắt trước cả chữ', () => {
    const colors = ['lakehouse', 'pipeline', 'sql_script', 'connection'].map((type) => {
      const { container } = render(<ItemTypeIcon type={type} />)
      return container.querySelector('svg')?.getAttribute('stroke')
    })
    expect(new Set(colors).size).toBe(4)
  })

  it('loại lạ vẫn render một icon, không phải khoảng trống', () => {
    const { container } = render(<ItemTypeIcon type="notebook" />)
    expect(container.querySelector('svg path')).toBeInTheDocument()
  })

  it('icon là aria-hidden — tên loại đã có dưới dạng chữ ngay cạnh', () => {
    const { container } = render(<ItemTypeIcon type="pipeline" />)
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden')
  })
})
