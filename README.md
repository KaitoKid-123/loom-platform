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

## Giai đoạn 2b — dịch vụ truy vấn

Chạy được `SELECT` trên bảng Iceberg qua HTTP, với cổng quyền của Giai đoạn 1 và năm giới
hạn tài nguyên. Chưa có giao diện — đó là 2c.

```
POST   /api/v1/query   { lakehouse_id, sql }  → 202 { query_id }   (hoặc 400 / 403)
GET    /api/v1/query/{id}                     → trạng thái + kết quả
DELETE /api/v1/query/{id}                     → huỷ, và nó dừng công việc thật
```

Trình duyệt chỉ nói chuyện với `loom-api`; `loom-query` là ClusterIP, không lộ ra ingress.
`loom-api` **tự tra** `workspace_id` từ `lakehouse_id` khi chuyển tiếp — giá trị client gửi
kèm LUÔN bị bỏ qua, nếu không thì cổng quyền chỉ canh một con số do chính kẻ gọi khai.

### Thành phần mới

| | |
|---|---|
| `packages/sqlkit` (`loom_sql`) | Đọc AST — `validate`, `dependencies`, `transpile`. **Không I/O**, có phép canh |
| `services/loom-query` | Pod ấm chạy DuckDB, 384Mi, không state |
| `POST /internal/authz/items` | `loom-query` **hỏi** quyền, không tự tính |
| `POST /internal/lakehouses/resolve` | Dịch tên lakehouse sang id, trong phạm vi một workspace |

### Ba ranh giới, và ai canh cái gì

**Quyền** — một nguồn luật duy nhất. Đường theo lô dùng lại `_chain_conditions` của Giai
đoạn 1 thay vì viết lại chuỗi tổ tiên, và có differential test đối chiếu lô-vs-đơn qua năm
cấu hình quyền.

**Nguồn dữ liệu** — `dependencies()` tách bảng catalog khỏi `external` (đường dẫn file, hàm
bảng). Cổng quyền từ chối `external`, trừ đúng một khe hẹp: `read_parquet`/`read_csv` với
đường dẫn **tương đối** trong `Files/` của chính lakehouse trong request.

**Mạng nội bộ** — `loom-api` gửi bí mật chia sẻ qua header, `loom-query` so bằng
`hmac.compare_digest`.

### Hai đường tới storage, và chúng khác nhau

```
Đường Iceberg   →  Lakekeeper cấp credential STS hẹp theo key-prefix của warehouse
Đường Files/    →  MinioStsProvider.for_workspace() → DuckDB httpfs, KHÔNG qua Iceberg
```

Mỗi item `lakehouse` ứng với một warehouse Lakekeeper, do `loom-api` tạo **trước khi** commit
hàng item — warehouse hỏng thì item không được tạo, thay vì để lại một lakehouse rỗng mà lỗi
chỉ lộ ra khi có người mở nó.

### Nợ đã biết sau Giai đoạn 2b

- **`loom-api` cầm credential gốc MinIO.** Giai đoạn 1 xây nó như một control plane không đọc
  secret nào; giờ nó giữ chìa khoá mở được mọi prefix của mọi workspace. Có phép canh AST
  (`services/api/tests/test_root_credential_guard.py`) khẳng định đúng một module chạm tới nó.
  Giai đoạn 6 tách việc cấp phát warehouse ra, hoặc dùng credential hẹp hơn nếu MinIO cho.
- **Bí mật chia sẻ không chống được pod đọc được chính Secret đó.** Cần ký lên principal hoặc
  mTLS — Giai đoạn 6.
- **`range()`/`generate_series()` bị từ chối.** Chúng không đọc dữ liệu từ đâu cả, nhưng
  sqlglot xếp chúng cùng nhóm với `read_parquet`. Nới ra là một quyết định riêng.
- **Warehouse mồ côi.** Nếu tạo warehouse xong mà tạo item hỏng, warehouse ở lại. Tốn chỗ,
  không gây hiểu nhầm — đổi một lỗi im lặng lấy một lỗi rác là có chủ đích.
- **Xoá mềm không xoá warehouse**, vì `restore` cần nó và Lakekeeper từ chối xoá warehouse
  còn bảng (`409`, `force=true` không vượt được). Và Giai đoạn 1b CHƯA có thao tác "bỏ-xoá"
  một item — một lakehouse bị xoá mềm hôm nay không có đường quay lại qua API.
- **`GET`/`DELETE /api/v1/query/{id}` không kiểm ai tạo ra query nào**, chỉ dựa vào việc
  `query_id` là UUID không đoán được (xem docstring `loom_query.routers.query`). Đủ cho phạm
  vi hiện tại, không phải một bất biến.
- **`loom-query` không có `live_update` trong Tiltfile** — sửa mã nguồn build lại ảnh đầy
  đủ, khác `loom-api`.

## Giai đoạn 2c — SQL editor, Lakehouse Explorer, và phép đo đóng giai đoạn

Giao diện cho mặt phẳng dữ liệu, cộng cửa chặn cuối của Giai đoạn 2.

- **Lakehouse Explorer** — cây namespace/bảng/cột, đọc qua `GET /api/v1/lakehouses/{id}/schema`.
  Endpoint tách tham số `depth` vì hai mức chênh nhau **200 lần** (7ms so với 1552ms): danh
  sách namespace rẻ, còn lấy cột của mọi bảng thì phải mở từng metadata Iceberg.
- **SQL editor** — chạy, huỷ, lưới kết quả, lưu thành item `sql_script` có version và
  `restore`, autocomplete tên bảng/cột theo lakehouse đang chọn.
- **Monaco tải TRÌ HOÃN.** Bundle khởi đầu 380 KB; Monaco là một chunk riêng 2.654 KB chỉ
  tải khi mở một `sql_script`. Canh bởi `web/scripts/check-bundle-splitting.mjs`.
- **`make smoke` lên 13 phép**, hai phép mới đi hết đường qua HTTP thật.

### Phép đo 2 — kết luận mà Giai đoạn 3 cần trước tiên

> **GIỮ PyIceberg. Không cắm Trino.** 50 GB thô (167,7 triệu dòng, 23,1 GB Parquet nén)
> ghi xong trong **00:34:04**, so với ngưỡng 60 phút đã chốt trước khi đo.

Tách theo giai đoạn, và chỗ này mới là phần đáng đọc:

| | |
|---|---|
| Sinh nguồn | 495s (24,2%) — chi phí của BÀI ĐO, không phải của nền tảng |
| Ghi Iceberg | 213s (**10,4%**) |
| Commit catalog | 1335s (**65,3%**) |

Câu hỏi đặt ra là "PyIceberg ghi Parquet có đủ nhanh không". Trả lời: thừa sức — 23 GB nén
trong 213 giây. Thứ tốn thời gian là **metadata**, và một engine tính toán khác không đụng
gì tới nó. Cắm Trino vào đây sẽ là sửa nhầm chỗ.

**Cần gạt thật cho Giai đoạn 3 là kích thước lô, không phải engine.** Chi phí commit gần
như cố định ~6,7s mỗi lần: 200 lô × 250 MB tốn 1335s, cũng ngần ấy dữ liệu chia thành
50 lô × 1 GB chỉ còn ~334s — cắt 17 phút khỏi tổng 34 phút mà không sửa một dòng mã.

Chi phí commit **có** tăng theo số snapshot, nhưng dưới tuyến tính: +11,9% sau 200 lần
commit. Đó đúng là rủi ro mà phép ngoại suy từ 10 lô không thể loại trừ, và giờ nó đã bị
loại trừ bằng số. Báo cáo đầy đủ:
`docs/measurements/2026-08-10-phase-2c-write-path-50gb.md`.

### Phép đo tìm ra một lỗi thật: MinIO bị OOMKilled

Lần chạy đầu chết ở lô 40/200. MinIO là Go, và **Go không đọc hạn mức cgroup** — bộ thu gom
rác nhắm theo GOGC, tức theo tốc độ phình của heap, nên nó phình qua trần 320Mi mà không
biết có trần. Sửa bằng `GOMEMLIMIT=352MiB` (giới hạn MỀM mà GC nhìn thấy) cộng limit cứng
448Mi; nâng limit không thôi chỉ làm nó chết muộn hơn.

Vì sao bốn phép đo RAM trước không thấy: tất cả đều đo lúc cụm **nghỉ**. MinIO nghỉ dùng
223 Mi, MinIO đang ghi dùng 271 Mi heap. Và `memory.current` một mình cũng không đủ — nó
gộp cả page cache, mà máy chủ lưu trữ thì luôn lấp đầy page cache một cách vô hại; phải
tách `anon` khỏi `file` mới thấy.

Ngân sách RAM mới, đo **trong lúc ghi**: **1500 / 1843 Mi**, còn dư 343 Mi.

### Nợ đã biết sau Giai đoạn 2c

- **Chưa tách được 6,7s mỗi lần commit thành ba phần**: chuyến đi tới Postgres của
  Lakekeeper trên Aiven, đọc lại manifest từ S3, và chính Lakekeeper. Khuyến nghị "lô to
  hơn" đúng bất kể tỷ lệ giữa chúng, nhưng muốn giảm chính con số đó thì phải tách trước.
- **Mỗi lần chạy `make smoke` để lại một bảng Iceberg vĩnh viễn** (`smoke_ns.ctas_result`).
  Xoá mềm workspace chỉ đặt một cột trong Postgres, và xoá warehouse qua Lakekeeper KHÔNG
  xoá object dưới S3. Nhỏ, nhưng không có giới hạn trên. Dọn cần một đường `DROP TABLE` mà
  API truy vấn chưa có.
- **Chuyển MinIO ra VPS giờ đáng giá hơn hẳn** — nó trả lại ~450 Mi cho cụm chứ không phải
  ~250 Mi như spec ước lượng trước khi đo.
- **Một CTAS mất ~11s ở local**, và gần hết chỗ đó là chuyến đi tới Aiven. Chấp nhận được
  cho một thao tác tạo bảng, nhưng nó đặt sàn cho mọi bài kiểm chạm đường ghi: `make smoke`
  giờ chờ tới `QUERY_TIMEOUT_S` (mặc định 60s) thay vì một trần 3 giây không có căn cứ.
