#!/usr/bin/env node
/**
 * Phép canh bundle của Giai đoạn 2c: chunk KHỞI ĐẦU (những gì trình duyệt tải trước khi
 * người dùng bấm gì) không được kéo theo Monaco.
 *
 * Monaco nặng 2-5MB — gấp mười tới hai mươi lần toàn bộ phần còn lại của bundle (~380KB,
 * xem commit message cho số đo thật). `React.lazy` ở `SqlEditorPanel.tsx` giữ nó ngoài
 * chunk khởi đầu HÔM NAY — `ItemPage.tsx` import `SqlEditorPanel` TĨNH, ranh giới lazy nằm
 * một tầng sâu hơn — nhưng lazy-loading chỉ đúng tới lần refactor đầu tiên vô tình đổi
 * `React.lazy(() => import(...))` thành `import { SqlEditor } from '...'` ở đầu file —
 * TypeScript/ESLint không tự cấm việc đó, chỉ có phép canh này.
 *
 * BA lớp, không phải một, để một cơ chế che bớt không làm phép canh "xanh giả":
 *
 *   1. MANIFEST — `dist/.vite/manifest.json` (bật bằng `build.manifest: true` trong
 *      `vite.config.ts`) phải nói `SqlEditor.tsx` là `isDynamicEntry` VÀ nằm trong
 *      `dynamicImports` của chunk entry. Hai trường NÀY, không phải trường `imports` —
 *      đã kiểm bằng thực nghiệm (bản build thật) rằng dưới Rolldown (bundler mặc định
 *      của Vite 8 dùng ở dự án này), `imports` trong manifest của các chunk KHÔNG mang ý
 *      nghĩa "chunk cha nào tải tĩnh chunk này" như manifest Rollup cổ điển — chunk
 *      `editor.api` tự liệt kê `imports: ["index.html"]`, tức CHIỀU NGƯỢC lại. Dựng một
 *      phép duyệt đồ thị dựa trên giả định sai đó sẽ tạo ra một phép canh "xanh giả" —
 *      đúng cạm bẫy dự án đã gặp. `isDynamicEntry`/`dynamicImports` thì KHÔNG mơ hồ.
 *   2. NỘI DUNG — chunk entry không được chứa chuỗi `MonacoEnvironment`. Monaco tự gán
 *      `self.MonacoEnvironment` (xem `SqlEditor.tsx`) bằng một property access ĐỘNG, nên
 *      minifier không đổi tên được nó — chuỗi này sống sót qua minify (đã kiểm bằng bản
 *      build thật, xem commit message). Lớp này ĐỘC LẬP hoàn toàn với manifest — nếu một
 *      thay đổi cấu hình chunking sau này làm lớp 1 đọc sai, lớp 2 vẫn bắt được bằng byte
 *      thật đã build ra.
 *   3. KÍCH THƯỚC — chunk entry phải dưới một ngưỡng generous (đủ chỗ cho ứng dụng lớn
 *      thêm nhiều, nhưng Monaco rò vào sẽ vượt ngưỡng đó gần SÁU LẦN, không phải xém).
 *
 * Chạy: `node scripts/check-bundle-splitting.mjs` SAU `npm run build` — hoặc `make
 * bundle-check`, đích mà `make web-test` gọi để CI chạy được.
 */

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DIST_DIR = join(WEB_ROOT, 'dist')
const MANIFEST_PATH = join(DIST_DIR, '.vite', 'manifest.json')

// Chuỗi Monaco tự dùng cho `self.MonacoEnvironment` — xem lớp 2 ở docstring trên.
const MONACO_MARKER = 'MonacoEnvironment'

// Module lazy DUY NHẤT hiện tại. Cập nhật đường dẫn này nếu `SqlEditorPanel.tsx` đổi chỗ
// import Monaco — phép canh khớp theo ĐƯỜNG DẪN NGUỒN trong manifest, không theo tên chunk
// đã băm hash (hash đổi mỗi build).
const EXPECTED_LAZY_MODULE = 'src/components/Editor/SqlEditor.tsx'

// Entry thật hôm nay ~380KB (xem commit message cho số đo). 700KB cho ứng dụng lớn thêm
// nhiều lần trước khi phải nâng ngưỡng — nhưng Monaco rò vào đẩy entry lên ~4MB, gấp gần
// SÁU LẦN ngưỡng này, không phải xém mức.
const MAX_ENTRY_BYTES = 700 * 1024

let failed = false
function fail(message) {
  console.error(`✗ bundle-check: ${message}`)
  failed = true
}

function readManifest() {
  let raw
  try {
    raw = readFileSync(MANIFEST_PATH, 'utf8')
  } catch {
    fail(
      `không đọc được ${MANIFEST_PATH} — chạy "npm run build" trước (cần build.manifest: true trong vite.config.ts).`,
    )
    return null
  }
  return JSON.parse(raw)
}

function findEntry(manifest) {
  const entry = Object.values(manifest).find((chunk) => chunk.isEntry)
  if (!entry) fail('không tìm thấy chunk entry (isEntry: true) nào trong manifest.')
  return entry ?? null
}

function main() {
  const manifest = readManifest()
  if (!manifest) return

  const entry = findEntry(manifest)
  if (!entry) return

  // 1. MANIFEST — hai trường KHÔNG mơ hồ, xem docstring trên cho lý do không dùng `imports`.
  const lazyModule = manifest[EXPECTED_LAZY_MODULE]
  if (!lazyModule) {
    fail(
      `không thấy "${EXPECTED_LAZY_MODULE}" trong manifest — đổi chỗ file thì sửa ` +
        `EXPECTED_LAZY_MODULE ở đầu script này.`,
    )
  } else if (!lazyModule.isDynamicEntry) {
    fail(
      `"${EXPECTED_LAZY_MODULE}" KHÔNG được đánh dấu \`isDynamicEntry\` trong manifest — ` +
        `nghĩa là nó bị \`import\` thẳng ở đâu đó thay vì qua \`React.lazy(() => import(...))\`.`,
    )
  }
  if (!(entry.dynamicImports ?? []).includes(EXPECTED_LAZY_MODULE)) {
    fail(
      `chunk entry KHÔNG liệt kê "${EXPECTED_LAZY_MODULE}" trong \`dynamicImports\` của nó.`,
    )
  }

  // 2. NỘI DUNG — backstop bằng byte thật đã build ra, độc lập với manifest.
  const entryFile = join(DIST_DIR, entry.file)
  const entryContent = readFileSync(entryFile, 'utf8')
  if (entryContent.includes(MONACO_MARKER)) {
    fail(
      `chunk entry (${entry.file}) chứa chuỗi "${MONACO_MARKER}" — Monaco đã lọt vào chunk ` +
        `khởi đầu dù manifest nói ngược lại. Nghi ngờ cấu hình chunking khác đang che phép canh.`,
    )
  }

  // 3. KÍCH THƯỚC — lưới an toàn cuối cùng.
  const entryBytes = Buffer.byteLength(entryContent, 'utf8')
  if (entryBytes > MAX_ENTRY_BYTES) {
    fail(
      `chunk entry (${entry.file}) nặng ${(entryBytes / 1024).toFixed(1)}KB, vượt ngưỡng ` +
        `${(MAX_ENTRY_BYTES / 1024).toFixed(0)}KB. Monaco (2-5MB) rò vào sẽ vượt ngưỡng này rất xa.`,
    )
  }

  if (failed) {
    process.exitCode = 1
    return
  }

  console.log(
    `✓ bundle-check: chunk entry (${entry.file}, ${(entryBytes / 1024).toFixed(1)}KB) không ` +
      `chứa Monaco — "${EXPECTED_LAZY_MODULE}" chỉ tới qua dynamicImports.`,
  )
}

main()
