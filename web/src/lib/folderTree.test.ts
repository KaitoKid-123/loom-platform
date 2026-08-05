import { describe, expect, it } from 'vitest'

import { type TreeItem, buildTree } from './folderTree'

const item = (name: string, folder: string): TreeItem => ({
  id: name,
  name,
  display_name: name,
  folder_path: folder,
  type: 'sql_script',
  version: 1,
})

describe('buildTree', () => {
  it('gộp item cùng folder vào một nhánh', () => {
    const tree = buildTree([item('a', '/'), item('b', '/')])
    expect(tree.folders).toEqual([])
    expect(tree.items.map((i) => i.name)).toEqual(['a', 'b'])
  })

  it('tạo folder trung gian dù không có item nào nằm trực tiếp trong đó', () => {
    // `/a/b/c/` có item, nhưng `/a/` và `/a/b/` thì không. Không tạo nút trung gian
    // thì item BIẾN MẤT khỏi cây — nó có folder_path mà không có đường dẫn tới, và
    // workspace trông rỗng trong khi item vẫn tồn tại.
    const tree = buildTree([item('x', '/a/b/c/')])
    expect(tree.folders.map((f) => f.name)).toEqual(['a'])
    expect(tree.folders[0].folders.map((f) => f.name)).toEqual(['b'])
    expect(tree.folders[0].folders[0].folders.map((f) => f.name)).toEqual(['c'])
    expect(tree.folders[0].folders[0].folders[0].items.map((i) => i.name)).toEqual(['x'])
    // Và item KHÔNG đồng thời nằm ở gốc — một bản cài đặt đặt nó ở cả hai chỗ cũng
    // thoả mọi khẳng định trên.
    expect(tree.items).toEqual([])
  })

  it('hai item cùng nhánh cha chia nhau nút trung gian, không tạo trùng', () => {
    // Tạo nút mới mỗi lần gặp là cây có hai folder `a` cạnh nhau, mỗi cái một item.
    const tree = buildTree([item('x', '/a/b/'), item('y', '/a/c/')])
    expect(tree.folders.map((f) => f.name)).toEqual(['a'])
    expect(tree.folders[0].folders.map((f) => f.name)).toEqual(['b', 'c'])
  })

  it('sắp folder theo bảng chữ cái, item theo display_name', () => {
    const tree = buildTree([
      item('zz', '/'),
      item('aa', '/'),
      item('trong-folder', '/m/'),
      item('cung-trong-folder', '/b/'),
    ])
    expect(tree.folders.map((f) => f.name)).toEqual(['b', 'm'])
    expect(tree.items.map((i) => i.name)).toEqual(['aa', 'zz'])
  })

  it('sắp theo display_name chứ không theo name', () => {
    // Người dùng đọc display_name. Sắp theo slug kỹ thuật cho ra thứ tự trông ngẫu
    // nhiên trên giao diện.
    const tree = buildTree([
      { ...item('z-slug', '/'), display_name: 'An' },
      { ...item('a-slug', '/'), display_name: 'Bình' },
    ])
    expect(tree.items.map((i) => i.display_name)).toEqual(['An', 'Bình'])
  })

  it.each(['', 'khong-co-gach-cheo', '//', '/a//b/', '/'])(
    'không nổ với folder_path lạ: %j',
    (bad) => {
      // Backend chặn bằng pattern, nhưng dữ liệu cũ hoặc gọi API trực tiếp có thể lọt.
      // Cây không được vỡ — item lệch chỗ còn hơn màn hình trắng.
      expect(() => buildTree([item('x', bad)])).not.toThrow()
    },
  )

  it('KHÔNG sinh folder tên rỗng từ đoạn rỗng', () => {
    // `not.toThrow()` ở trên một mình không thấy được điều này: `/a//b/` chạy êm mà
    // cho ra một nút không tên, và trên giao diện đó là một dòng trống bấm được.
    const collect = (node: { name: string; folders: { name: string }[] }): string[] => [
      node.name,
      ...node.folders.flatMap((f) => collect(f as never)),
    ]
    const tree = buildTree([item('x', '/a//b/'), item('y', '//'), item('z', '')])
    // Nút gốc tên rỗng là đúng; mọi nút KHÁC phải có tên.
    expect(collect(tree).slice(1).filter((n) => n === '')).toEqual([])
  })

  it('đường dẫn tuyệt đối của mỗi folder khớp folder_path để deep-link được', () => {
    const tree = buildTree([item('x', '/a/b/')])
    expect(tree.path).toBe('/')
    expect(tree.folders[0].path).toBe('/a/')
    expect(tree.folders[0].folders[0].path).toBe('/a/b/')
  })

  it('cây rỗng khi không có item nào, không phải null', () => {
    const tree = buildTree([])
    expect(tree.folders).toEqual([])
    expect(tree.items).toEqual([])
  })
})
