import { useParams, useSearchParams } from 'react-router'

/**
 * Khung tối thiểu để route tồn tại từ Task 26; Task 29 làm cây thật.
 *
 * Đọc `folder` và `type` từ QUERY STRING ngay từ đầu, không từ state React: đó là
 * quy tắc "URL là state" của spec mục 7.4, và chuyển sau khi cây đã dựng là viết lại.
 */
export function ExplorerPage() {
  const { workspaceId } = useParams()
  const [params] = useSearchParams()
  return (
    <section>
      <h1 className="text-lg font-medium">Explorer</h1>
      <p className="mt-2 text-sm text-dim">
        {workspaceId} · folder={params.get('folder') ?? '/'} · type={params.get('type') ?? 'tất cả'}
      </p>
    </section>
  )
}
