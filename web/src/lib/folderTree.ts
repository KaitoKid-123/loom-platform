export interface TreeItem {
  id: string
  name: string
  display_name: string
  folder_path: string
  type: string
  version: number
}

export interface TreeNode {
  name: string
  /** Đường dẫn TUYỆT ĐỐI, luôn có `/` ở đầu và cuối — khớp đúng `folder_path`. */
  path: string
  folders: TreeNode[]
  items: TreeItem[]
}

function emptyNode(name: string, path: string): TreeNode {
  return { name, path, folders: [], items: [] }
}

/**
 * Dựng cây từ `folder_path` — một CHUỖI, không phải quan hệ cha-con.
 *
 * Hai chỗ dễ sai, cả hai đã kiểm bằng cách phá:
 *
 * 1. **Phải tự sinh folder trung gian.** Item ở `/a/b/c/` cần ba nút dù không có item
 *    nào nằm trực tiếp trong `/a/` hay `/a/b/`. Thiếu chúng thì item biến mất khỏi cây
 *    trong khi vẫn tồn tại, và workspace trông rỗng.
 * 2. **Phải lọc đoạn rỗng.** `/a//b/` và `''` không được sinh ra một nút không tên —
 *    trên giao diện đó là một dòng trống bấm được. Backend chặn bằng pattern, nhưng
 *    dữ liệu cũ và lời gọi API trực tiếp vẫn lọt.
 */
export function buildTree(items: TreeItem[]): TreeNode {
  const root = emptyNode('', '/')

  for (const item of items) {
    const segments = (item.folder_path || '/').split('/').filter(Boolean)

    let node = root
    let path = '/'
    for (const segment of segments) {
      path += `${segment}/`
      // Tìm lại nút đã có thay vì tạo mới: hai item ở `/a/b/` và `/a/c/` phải chia
      // nhau nút `a`, không sinh hai nút `a` cạnh nhau.
      let child = node.folders.find((f) => f.name === segment)
      if (!child) {
        child = emptyNode(segment, path)
        node.folders.push(child)
      }
      node = child
    }
    node.items.push(item)
  }

  const sortNode = (node: TreeNode): void => {
    node.folders.sort((a, b) => a.name.localeCompare(b.name, 'vi'))
    // Theo `display_name`, không theo `name`: người dùng đọc display_name, còn sắp
    // theo slug kỹ thuật cho ra thứ tự trông ngẫu nhiên trên giao diện.
    node.items.sort((a, b) => a.display_name.localeCompare(b.display_name, 'vi'))
    node.folders.forEach(sortNode)
  }
  sortNode(root)
  return root
}
