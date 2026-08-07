# Loom

Nền tảng dữ liệu hợp nhất nội bộ. Xem `docs/superpowers/specs/` (không nằm trong repo) để biết thiết kế.

## Bắt đầu

    rm -rf .venv       # thư mục này còn sót từ dự án cũ, xoá trước lần sync đầu tiên
    make sync          # cài dependency Python
    make web-install   # cài dependency frontend
    make test          # chạy unit test

Lệnh `make help` liệt kê mọi thứ.

`make lint` chỉ có ý nghĩa từ Task 2 trở đi — trước đó chưa có mã Python nào.

## Vòng lặp phát triển

    make dev     # dựng cụm k3d + Dex + Secret Aiven, rồi chạy Tilt (hot reload)
    make smoke   # mười một phép kiểm chấp nhận qua HTTP, với môi trường đang sống

Hai điều về Tilt đã tốn thời gian gỡ, ghi lại vì không ai đoán được:

- **Dừng `tilt up` xoá theo `loom-api` và `loom-web`.** Ctrl-C không chỉ tắt giao
  diện — nó hạ luôn thứ Tilt đã triển khai. `dex` sống sót vì do `make infra` tạo.
  Muốn giữ app chạy mà không giữ Tilt thì dùng `helm upgrade --install` (xem cuối
  `Tiltfile`), đổi lại mất hot reload.
- **Tilt giữ bốn port trên host:** `8080`, `8000`, `10350` và một port ephemeral. Nếu
  `8080` đang bị chiếm thì `tilt up` hỏng ở bước port-forward, không phải ở bước
  build, và thông báo lỗi không chỉ về phía port.

`make smoke` chạy NGAY sau một lần rollout vẫn phải 11/11 — `preStop` trong chart giữ
pod phục vụ tới khi endpoint được gỡ khỏi Service. Nếu nó hỏng lẻ tẻ sau khi deploy
thì thứ cần xem là hook đó, không phải test.

## Giai đoạn 1 — mặt phẳng điều khiển

Workspace và item (bốn loại) có version và ETag, RBAC bốn vai trò × bốn phạm vi cho cả
người và nhóm, audit ghi cùng transaction với thay đổi, và một giao diện đọc/ghi được:
Explorer, ⌘K, hộp thoại quyền, Connections.

Admin đầu tiên phải được gán từ ngoài hệ thống — mọi thứ khác cấp quyền qua API, mà API
đòi người gọi đã có quyền:

    make grant-admin EMAIL=long@loom.local

### Nợ đã biết sau Giai đoạn 1

- Explorer tải một trang 200 item; workspace lớn hơn hiện cảnh báo nhưng cây chưa phân trang
- Đổi nhóm ở IdP chỉ có hiệu lực ở lần đăng nhập sau — nhóm được chụp vào session
- **Chưa có endpoint tra người dùng**, nên hộp thoại quyền phải nhập UUID để gán cho một
  người; gán cho nhóm thì chỉ cần tên nhóm
- `resource_profile` được lưu nhưng chưa có gì đọc nó — cần scheduler ở Giai đoạn 3
- Assignment có thể mồ côi nếu scope bị xoá cứng; cần task dọn ở Giai đoạn 6
- Chưa có UI thùng rác; phục hồi item đã xoá phải qua API
- Trang chi tiết item (`/workspaces/{ws}/items/{id}`) chưa có nội dung — Explorer liên kết
  tới nó nhưng Giai đoạn 2 mới làm trình soạn thảo

## Giai đoạn 2a — nền storage

MinIO và Lakekeeper (Iceberg REST catalog) chạy trong cụm; `packages/storagekit` cấp
credential S3 ngắn hạn hẹp theo prefix của workspace; `packages/icebergkit` là chỗ duy
nhất trong hệ thống biết Iceberg tồn tại — mọi thứ khác nói chuyện với nó bằng Arrow.

| Lệnh | Làm gì |
|---|---|
| `make minio-console` | Console MinIO ở http://localhost:9001 |
| `make minio-s3` | Port-forward cổng S3 ra localhost:9000 |
| `make ram` | RAM từng pod và CẢ NODE, so với trần 1,8 GB |
| `make measure-scan` | Phép đo 1 — thời gian lập kế hoạch quét bảng Iceberg |
| `make measure-spill` | Phép đo 1 — DuckDB trong cgroup 384Mi thật |

**MinIO chỉ chạy ở local** (`deploy/infra/minio.yaml`, cùng khuôn với Dex). dev/prod trỏ
endpoint ngoài — kế hoạch là một VPS riêng.

**Lakekeeper cần một database THỨ HAI** trên cùng Aiven service. Khôi phục Loom là khôi
phục **cả hai** database về cùng một mốc — xem `docs/runbook/restore.md`.

### Ba con số bàn giao cho Giai đoạn 2b

- **Lập kế hoạch quét p95 = 172,7ms** (1 triệu dòng, 20 snapshot, container cùng máy).
  Dưới ngưỡng 200ms nên **không lấp `MetadataCache`**. Nhưng nó ngang ngửa thời gian đọc
  dữ liệu, và đây là cận dưới — VPS sẽ thêm một chặng mạng vào đúng chỗ nhạy nhất.
- **DuckDB phải ghim CẢ `memory_limit` LẪN `threads`.** Nó không đọc cgroup, và nhu cầu
  bộ nhớ co giãn theo số luồng: cùng query, cùng hạn mức, `threads=2` chạy xong còn
  `threads=4` thì OOM. Con số đã kiểm: `memory_limit=256MB`, `threads=2`, container
  384Mi, RSS đỉnh 348 Mi.
- **RAM cả node 1318 / 1843 Mi.** Còn dư 525 Mi; `loom-query` chiếm ~348 Mi khi tải nặng.

### Nợ đã biết sau Giai đoạn 2a

- **Chưa có kế hoạch sao lưu lakehouse.** Đây là món mất khi chọn MinIO thay Backblaze B2.
  Ở 2a dữ liệu là dữ liệu thử nên chấp nhận được, nhưng nó là **điều kiện** của việc đưa
  dữ liệu thật vào và của việc chuyển sang VPS.
- `authz-backend` của Lakekeeper là `allow-all`. Chấp nhận được vì Service là ClusterIP,
  không lộ ra ingress — nhưng cần xem lại ở Giai đoạn 6.
- Credential gốc của MinIO ở local là hằng số trong `deploy/infra/minio.yaml`. dev/prod
  lấy từ Vault qua External Secrets.
- Xoay credential vẫn là việc tay tới Giai đoạn 6.
