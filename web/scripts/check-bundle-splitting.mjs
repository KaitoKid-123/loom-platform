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
 * Giai đoạn 3c thêm một phép canh THỨ HAI vào file này, KHÔNG liên quan tới ba lớp Monaco ở
 * trên: danh sách dependency runtime cho phép (`ALLOWED_DEPENDENCIES` / `checkDependencies`
 * bên dưới), giữ cam kết "0 gói mới" của 3c hiện ra thành lỗi rõ ràng khi có ai thêm một
 * gói mà không sửa danh sách này. Nó nằm chung file với ba lớp Monaco vì cả hai đều là phép
 * canh build-time mà `make bundle-check` chạy và CI chỉ nối MỘT hook tới — không phải vì
 * chúng kiểm cùng một thứ. (Nếu một file kiểm hai mối lo không liên quan là điều đáng ngại,
 * đó là việc cần bàn riêng, không phải lý do để tài liệu này nói sai file làm gì.)
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

// Bảy dependency runtime, và con số này CẦN một lý do để đổi.
//
// Giai đoạn 3c dựng Pipeline Designer và Monitor Hub mà không thêm gói nào: trình soạn là
// một danh sách (không cần thư viện đồ thị), sơ đồ là `div` + một SVG (không cần React
// Flow), dải trạng thái là `div` (không cần thư viện chart). Danh sách CỐ ĐỊNH chứ không
// phải một số đếm: một số đếm cho phép đổi gói này bằng gói khác mà phép canh không thấy.
//
// Khi một giai đoạn sau thêm dependency thật, người thêm phải sửa danh sách này — và đó
// chính là điểm của phép canh. Nó không cấm; nó bắt việc thêm phải là một quyết định
// hiện ra trong diff.
const ALLOWED_DEPENDENCIES = [
  '@fontsource-variable/ibm-plex-sans',
  '@fontsource/ibm-plex-mono',
  '@tanstack/react-query',
  'monaco-editor',
  'react',
  'react-dom',
  'react-router',
]

let failed = false
function fail(message) {
  console.error(`✗ bundle-check: ${message}`)
  failed = true
}

// Đặt SAU `fail` (giống `readManifest`/`findEntry` bên dưới) — không đặt trước như bản đầu,
// vì hàm này gọi `fail`, và nếu lỡ có ai gọi `checkDependencies()` ở top-level thay vì bên
// trong `main()`, đặt trước `let failed = false` sẽ ném `ReferenceError: Cannot access
// 'failed' before initialization` thay vì thông báo `✗ bundle-check:` sạch sẽ — một phép
// canh crash thay vì nói rõ vấn đề thì tệ hơn không có phép canh.
function checkDependencies() {
  const pkgPath = join(WEB_ROOT, 'package.json')
  const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'))
  const actual = Object.keys(pkg.dependencies ?? {}).sort()
  const expected = [...ALLOWED_DEPENDENCIES].sort()

  const added = actual.filter((name) => !expected.includes(name))
  const removed = expected.filter((name) => !actual.includes(name))

  if (added.length > 0) {
    fail(
      `dependency runtime MỚI trong web/package.json: ${added.join(', ')}. ` +
        `Giai đoạn 3c cam kết 0 gói mới. Nếu việc thêm là có chủ đích, thêm tên vào ` +
        `ALLOWED_DEPENDENCIES ở scripts/check-bundle-splitting.mjs kèm lý do.`,
    )
  }
  if (removed.length > 0) {
    fail(
      `dependency đã BIẾN MẤT khỏi web/package.json: ${removed.join(', ')}. ` +
        `Nếu việc bỏ là có chủ đích, xoá tên khỏi ALLOWED_DEPENDENCIES; nếu không — gói bị ` +
        `hạ xuống devDependencies hay bị bỏ nhầm — thì khôi phục nó trong dependencies, vì ` +
        `chính việc bỏ đó mới là lỗi.`,
    )
  }
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
  // Chạy TRƯỚC phần đọc manifest: phép canh này không cần bản build, nên nó vẫn nói được
  // điều gì đó khi `npm run build` hỏng.
  checkDependencies()

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
    `✓ bundle-check: ${ALLOWED_DEPENDENCIES.length} dependency runtime, đúng danh sách cho phép.`,
  )
  console.log(
    `✓ bundle-check: chunk entry (${entry.file}, ${(entryBytes / 1024).toFixed(1)}KB) không ` +
      `chứa Monaco — "${EXPECTED_LAZY_MODULE}" chỉ tới qua dynamicImports.`,
  )
}

main()
