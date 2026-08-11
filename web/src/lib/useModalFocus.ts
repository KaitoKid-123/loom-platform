import { useEffect, useRef } from 'react'

/** Phần tử nhận được tiêu điểm bằng Tab. `[tabindex="-1"]` CỐ Ý bị loại: nó nhận
 *  được `focus()` gọi bằng mã nhưng KHÔNG nằm trong vòng Tab, nên gộp nó vào đây
 *  làm bẫy tiêu điểm dừng ở một phần tử mà người dùng không tự Tab tới được. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Quản lý tiêu điểm cho một hộp thoại: đưa vào, giữ lại, trả về, và đóng bằng Escape.
 *
 * Ba hộp thoại của Loom trước đây có `role="dialog"` + `aria-modal="true"` + một
 * handler Escape, và **cả ba đều không dùng được bằng bàn phím**. Từng thứ một:
 *
 * 1. `aria-modal` chỉ nói với cây trợ năng rằng phần còn lại của trang bị che. Nó
 *    KHÔNG di chuyển tiêu điểm, nên người dùng trình đọc màn hình không biết có gì
 *    vừa mở ra.
 * 2. Nó cũng KHÔNG chặn Tab. Không có bẫy, Tab đi xuyên qua hộp thoại ra các nút
 *    nằm sau lớp phủ — người dùng thao tác lên những thứ họ không nhìn thấy.
 * 3. Handler Escape nằm trên `onKeyDown` của chính div hộp thoại. React chỉ nhận sự
 *    kiện bàn phím NỔI LÊN từ phần tử đang có tiêu điểm; vì (1) không đưa tiêu điểm
 *    vào, sự kiện không bao giờ đi qua div đó. Mã bắt Escape có tồn tại và không
 *    chạy — cho tới khi người dùng bấm chuột vào trong. Đó là dạng lỗi tệ nhất: đọc
 *    mã thì thấy đã xử lý rồi.
 *
 * Nên hook này gộp cả bốn việc. Tách Escape ra để nguyên chỗ cũ sẽ giữ lại đúng lỗi
 * (3), vì nó là hệ quả của việc không quản lý tiêu điểm.
 *
 * `listener` gắn ở cấp `document`, không phải trên phần tử: Escape phải chạy được
 * bất kể tiêu điểm đang ở đâu.
 */
export function useModalFocus<T extends HTMLElement>(onClose: () => void) {
  const ref = useRef<T | null>(null)

  // Giữ `onClose` trong ref để effect chính KHÔNG phụ thuộc vào nó. Chỗ gọi thường
  // truyền một closure mới mỗi lần render (`onClose={() => setOpen(false)}`), nên
  // để nó trong danh sách phụ thuộc sẽ tháo và gắn lại listener mỗi lần render — và
  // tệ hơn, chạy lại phần "đưa tiêu điểm vào", cướp tiêu điểm khỏi ô người dùng
  // đang gõ ở mỗi ký tự.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const container = ref.current
    if (container === null) return

    const previouslyFocused = document.activeElement as HTMLElement | null

    // KHÔNG lọc theo `offsetParent !== null` để bỏ qua phần tử ẩn: `offsetParent`
    // LUÔN là `null` trong jsdom (nó không tính layout), nên bộ lọc đó làm danh
    // sách rỗng sạch trong test — và một bẫy tiêu điểm không có phần tử nào thì
    // không bẫy gì. Đã thử và bị chính test ở đây bắt.
    //
    // `:disabled` trong bộ chọn đã loại phần lớn trường hợp thật. Nếu sau này có
    // hộp thoại chứa nhánh ẩn bằng CSS, dùng `hidden`/`inert` — hai thứ đó jsdom
    // hiểu — chứ đừng quay lại `offsetParent`.
    const focusable = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => !element.hasAttribute('hidden') && element.closest('[inert]') === null,
      )

    // Đưa tiêu điểm vào phần tử đầu tiên. Nếu hộp thoại chưa có phần tử nào nhận
    // được tiêu điểm (đang tải), đặt lên chính container qua `tabindex=-1` để trình
    // đọc màn hình ít nhất công bố được nó.
    const first = focusable()[0]
    if (first !== undefined) {
      first.focus()
    } else {
      container.setAttribute('tabindex', '-1')
      container.focus()
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return

      const elements = focusable()
      if (elements.length === 0) {
        event.preventDefault()
        return
      }

      const firstElement = elements[0]!
      const lastElement = elements[elements.length - 1]!
      const active = document.activeElement

      // Chỉ can thiệp ở hai ĐẦU vòng. Ở giữa, để trình duyệt tự xử lý — nó biết thứ
      // tự Tab tốt hơn bất kỳ bản cài tay nào (phần tử ẩn, `contenteditable`,
      // shadow DOM).
      if (event.shiftKey && (active === firstElement || !container.contains(active))) {
        event.preventDefault()
        lastElement.focus()
      } else if (!event.shiftKey && active === lastElement) {
        event.preventDefault()
        firstElement.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      // Trả tiêu điểm về chỗ cũ. `isConnected` vì phần tử mở hộp thoại có thể đã bị
      // gỡ khỏi DOM trong lúc hộp thoại còn mở (xoá một item rồi đóng hộp thoại) —
      // gọi `focus()` lên một node mồ côi thì im lặng không làm gì, và tiêu điểm rơi
      // về `<body>`.
      if (previouslyFocused?.isConnected) previouslyFocused.focus()
    }
  }, [])

  return ref
}
