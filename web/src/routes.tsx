import type { RouteObject } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { ConnectionsPage } from './pages/ConnectionsPage'
import { ExplorerPage } from './pages/ExplorerPage'
import { ItemPage } from './pages/ItemPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { WorkspaceListPage } from './pages/WorkspaceListPage'

/**
 * URL là state — quy tắc bắt buộc của spec mục 7.4.
 *
 * Mọi thứ người dùng đang xem phải deep-link được: một workspace, một folder, một
 * bộ lọc. Bộ lọc nằm trong QUERY STRING chứ không trong state React, để người dùng
 * gửi được đường dẫn cho đồng nghiệp và F5 không mất chỗ đang đứng.
 *
 * Làm ngay từ đầu vì thêm sau khi đã có bốn màn hình là viết lại cả bốn.
 */
export const routeObjects: RouteObject[] = [
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <WorkspaceListPage /> },
      { path: 'workspaces/:workspaceId/items', element: <ExplorerPage /> },
      // Không có route này thì mọi cú bấm item trong Explorer và mọi Enter trong ⌘K đều
      // rơi vào route bắt-tất-cả và ra trang "không tìm thấy" — một hành trình vỡ, dù cả
      // hai chỗ kia đều có test xanh.
      { path: 'workspaces/:workspaceId/items/:itemId', element: <ItemPage /> },
      { path: 'workspaces/:workspaceId/connections', element: <ConnectionsPage /> },
      // Bắt mọi đường lạ. Đã kiểm bằng cách gỡ dòng này ra, và kết quả KHÔNG phải
      // màn hình trắng như tưởng: react-router 8 render trang lỗi mặc định của
      // chính nó — "Unexpected Application Error! / 404 Not Found / 💿 Hey
      // developer 👋", tiếng Anh, nói với lập trình viên, kèm hướng dẫn thêm
      // errorElement, và không có đường nào về. Với người dùng thật thì đó là một
      // trang gỡ lỗi của framework, tệ hơn cả một trang trắng vì nó trông như hệ
      // thống vừa vỡ.
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
