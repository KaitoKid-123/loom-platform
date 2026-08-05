import { useState } from 'react'

import { ProblemError } from '../../lib/problem'
import { useCreateItem } from '../../lib/useItemMutations'

const TYPES = ['sql_script', 'pipeline', 'lakehouse', 'connection'] as const
type ItemType = (typeof TYPES)[number]

const CONNECTION_KINDS = ['postgres', 'mysql', 'sqlserver', 'rest'] as const

/**
 * `definition` mặc định cho từng loại, khớp `DEFAULT_DEFINITION` ở backend.
 *
 * `connection` KHÔNG có mặc định, đúng như backend: không đoán được host hay secret_ref
 * của ai. Form hỏi từng ô.
 */
function defaultDefinition(type: ItemType): Record<string, unknown> {
  switch (type) {
    case 'sql_script':
      return { schema_version: 1, sql: '' }
    case 'pipeline':
      return { schema_version: 1, nodes: [], edges: [] }
    case 'lakehouse':
      return { schema_version: 1 }
    case 'connection':
      return { schema_version: 1 }
  }
}

interface Props {
  workspaceId: string
  onClose: () => void
}

export function NewItemDialog({ workspaceId, onClose }: Props) {
  const [type, setType] = useState<ItemType>('sql_script')
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [folderPath, setFolderPath] = useState('/')
  const [kind, setKind] = useState<(typeof CONNECTION_KINDS)[number]>('postgres')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('5432')
  const [database, setDatabase] = useState('')
  const [secretRef, setSecretRef] = useState('')

  const create = useCreateItem(workspaceId)

  // Lỗi từng trường từ `errors[]` của backend, gắn vào ĐÚNG ô. Không có nó thì người
  // dùng đọc một câu chung và phải tự đoán ô nào sai trong sáu ô.
  const fieldErrors =
    create.error instanceof ProblemError ? create.error.fieldErrors : ({} as Record<string, string>)
  const generalError =
    create.error && !(create.error instanceof ProblemError && Object.keys(fieldErrors).length > 0)
      ? create.error.message
      : null

  const submit = () => {
    const definition = defaultDefinition(type)
    if (type === 'connection') {
      Object.assign(definition, {
        kind,
        host,
        port: Number(port),
        ...(database ? { database } : {}),
        secret_ref: secretRef,
      })
    }
    create.mutate(
      {
        type,
        name,
        display_name: displayName || name,
        folder_path: folderPath,
        definition,
      },
      { onSuccess: onClose },
    )
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="new-item-title"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-8"
      // Escape đóng: một hộp thoại không đóng được bằng bàn phím là một cái bẫy cho
      // người dùng không dùng chuột.
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
        className="w-full max-w-lg space-y-3 rounded-lg border border-line bg-surface p-6"
      >
        <h2 id="new-item-title" className="text-lg font-medium">
          Tạo item
        </h2>

        <Field label="Loại item" htmlFor="new-type">
          <select
            id="new-type"
            value={type}
            onChange={(e) => setType(e.target.value as ItemType)}
            className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
          >
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Tên kỹ thuật" htmlFor="new-name" error={fieldErrors.name}>
          <input
            id="new-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            // `pattern` khớp ĐÚNG regex của backend, và nó là gợi ý sớm chứ không phải
            // phép kiểm: server vẫn là chỗ chặn. Sai khác giữa hai bên chỉ làm người
            // dùng bị từ chối ở một chỗ mà bên kia cho qua.
            pattern="[a-z0-9][a-z0-9-]*"
            title="chữ thường, số và dấu gạch ngang; không bắt đầu bằng gạch ngang"
            required
            className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
          />
        </Field>

        <Field label="Tên hiển thị" htmlFor="new-display" error={fieldErrors.display_name}>
          <input
            id="new-display"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="mặc định lấy theo tên kỹ thuật"
            className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
          />
        </Field>

        <Field label="Folder" htmlFor="new-folder" error={fieldErrors.folder_path}>
          <input
            id="new-folder"
            value={folderPath}
            onChange={(e) => setFolderPath(e.target.value)}
            pattern="/([^/]+/)*"
            title="phải bắt đầu và kết thúc bằng /"
            className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
          />
        </Field>

        {type === 'connection' && (
          <fieldset className="space-y-3 rounded border border-line p-3">
            <legend className="px-1 text-sm text-dim">Kết nối</legend>

            <Field label="Loại nguồn" htmlFor="new-kind" error={fieldErrors.kind}>
              <select
                id="new-kind"
                value={kind}
                onChange={(e) => setKind(e.target.value as typeof kind)}
                className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
              >
                {CONNECTION_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Host" htmlFor="new-host" error={fieldErrors.host}>
              <input
                id="new-host"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                required
                className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
              />
            </Field>

            <Field label="Port" htmlFor="new-port" error={fieldErrors.port}>
              <input
                id="new-port"
                type="number"
                min={1}
                max={65535}
                value={port}
                onChange={(e) => setPort(e.target.value)}
                required
                className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
              />
            </Field>

            <Field label="Database" htmlFor="new-database" error={fieldErrors.database}>
              <input
                id="new-database"
                value={database}
                onChange={(e) => setDatabase(e.target.value)}
                className="w-full rounded border border-line bg-surface px-2 py-1 text-sm"
              />
            </Field>

            <Field label="Secret ref" htmlFor="new-secret" error={fieldErrors.secret_ref}>
              <input
                id="new-secret"
                // CỐ Ý không `type="password"`. Đây là một THAM CHIẾU tới secret, không
                // phải secret. Che nó bằng dấu sao dạy người dùng rằng ô này nhận mật
                // khẩu, và đó đúng là thứ mà `_check_ref` ở backend phải chặn.
                value={secretRef}
                onChange={(e) => setSecretRef(e.target.value)}
                placeholder="vault://path#key hoặc k8s://namespace/name#key"
                required
                className="w-full rounded border border-line bg-surface px-2 py-1 font-mono text-sm"
              />
              <p className="mt-1 text-xs text-dim">
                Đường dẫn tới secret, không phải mật khẩu. Mật khẩu dán vào đây sẽ bị từ
                chối — và nếu lọt thì nó đi vào lịch sử version, audit và Git.
              </p>
            </Field>
          </fieldset>
        )}

        {generalError && (
          <p role="alert" className="text-sm text-dim">
            {generalError}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-3 py-1 text-sm text-dim hover:bg-muted"
          >
            Huỷ
          </button>
          <button
            type="submit"
            disabled={create.isPending}
            className="rounded border border-line px-3 py-1 text-sm hover:bg-muted disabled:opacity-50"
          >
            {create.isPending ? 'Đang tạo…' : 'Tạo'}
          </button>
        </div>
      </form>
    </div>
  )
}

function Field({
  label,
  htmlFor,
  error,
  children,
}: {
  label: string
  htmlFor: string
  error?: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1 block text-sm">
        {label}
      </label>
      {children}
      {error && (
        <p role="alert" className="mt-1 text-xs text-red-400">
          {error}
        </p>
      )}
    </div>
  )
}
