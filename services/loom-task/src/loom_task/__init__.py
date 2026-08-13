"""`loom-task` — tiến trình chạy MỘT lần cho MỘT `ingest_run` rồi chết.

Không server, không vòng lặp chờ việc, không lịch: Kubernetes phóng một `Job`
cho mỗi lần nạp (`loom_api.jobs.JobLauncher`, `backoff_limit=0`) và tiến trình
này đọc `run_id` của mình từ biến môi trường. Đó là lý do nó KHÔNG có `main.py`
kiểu ứng dụng dài hạn: mọi trạng thái đều nằm ở control plane, nên một pod chết
không mang theo thông tin nào không thể đọc lại được.

Nó KHÔNG có credential Postgres của control plane. Mọi thứ nó cần đọc và mọi
thứ nó cần ghi đi qua ba route `/internal/ingest/*` — xem `client.py`.
"""
