import { useParams } from 'react-router'

/** Khung tối thiểu để route tồn tại từ Task 26; Task 33 làm nội dung thật. */
export function ConnectionsPage() {
  const { workspaceId } = useParams()
  return (
    <section>
      <h1 className="text-lg font-medium">Connections</h1>
      <p className="mt-2 text-sm text-dim">{workspaceId}</p>
    </section>
  )
}
