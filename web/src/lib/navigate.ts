/** Một chỗ duy nhất rời khỏi SPA — nhờ vậy test giả lập được. */
export function navigateTo(url: string): void {
  window.location.assign(url)
}
