"""Tên header mang bí mật chia sẻ giữa `loom-api` và `loom-query` (Task 10/11).

`loom-query` không có OIDC/session riêng (xem docstring `loom_query.main`) —
nó nhận principal của người dùng cuối ngay trong thân request. ClusterIP chỉ
chặn traffic TỪ NGOÀI cluster; bất kỳ pod nào khác TRONG cùng namespace vẫn
POST thẳng được tới nó và tự xưng là bất kỳ ai. Header này là lớp chặn cho
khoảng hở đó: `loom-api` đính kèm bí mật ở MỌI request gửi sang `loom-query`,
và `loom-query` từ chối (401) bất kỳ request nào thiếu hoặc sai nó — xem
`loom_query.security.require_shared_secret`.

Một hằng số DÙNG CHUNG, không phải hai chuỗi viết tay ở hai service: cả hai
đều đã phụ thuộc `loom-core` (xem `pyproject.toml` của từng service), nên đây
là chỗ trung lập duy nhất mà một lần đổi tên header không có cơ hội lệch giữa
bên gửi và bên kiểm.

**Nợ đã biết, ghi ra thay vì lờ đi (spec Giai đoạn 2b):** bí mật chia sẻ chỉ
chứng minh request tới TỪ MỘT NGUỒN CÓ Secret này — nó không chống được một
pod khác ĐỌC ĐƯỢC chính Secret đó (ví dụ qua một lỗ RBAC của Kubernetes cho
phép pod đọc Secret trong namespace). Chống điều đó cần ký lên principal
(chữ ký, không phải một bí mật tĩnh dùng lại) hoặc mTLS giữa hai service —
Giai đoạn 6. README của `services/loom-query` ghi lại đúng giới hạn này.
"""

QUERY_SHARED_SECRET_HEADER = "X-Loom-Query-Secret"  # noqa: S105 — tên header, không phải giá trị bí mật

# Bí mật của ĐƯỜNG NGƯỢC LẠI (Giai đoạn 3a, Task 10): pod nạp -> `loom-api`,
# trên `/internal/ingest/*`. Xem `loom_api.internal_security`.
#
# Một header RIÊNG cho một bí mật RIÊNG, không dùng lại tên ở trên. Hai đường
# này khác nhau ở cả ba mặt — ai gửi, ai kiểm, và giá trị nào — nên một tên
# chung sẽ chỉ mời hai bên nối nhầm vào nhau và biến việc TÁCH bí mật (lý do
# `ingest-shared-secret` tồn tại, xem `loom_core.config.Settings.
# task_shared_secret_key`) thành một sự tách chỉ có trên giấy.
INGEST_SHARED_SECRET_HEADER = "X-Loom-Ingest-Secret"  # noqa: S105 — tên header, không phải giá trị

# Ba bí mật chia sẻ — MỖI THỨ một bí mật RIÊNG:
#
# LOOM_INGEST_SHARED_SECRET  → ai giữ nó thì khởi động được ingest
# LOOM_QUERY_SHARED_SECRET   → ai giữ nó thì mạo danh được bất kỳ principal nào (X-Loom-Run-As)
# LOOM_SCHEDULE_SHARED_SECRET → ai giữ nó thì khởi động được scheduled pipeline runs
#
# Dùng chung = gộp quyền ≠ vào một thứ → mất kiểm soát.

SCHEDULE_SHARED_SECRET_HEADER = "X-Loom-Schedule-Secret"  # noqa: S105 — tên header, không phải giá trị bí mật
