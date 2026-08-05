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
    make smoke   # chín phép kiểm chấp nhận qua HTTP, với môi trường đang sống

Hai điều về Tilt đã tốn thời gian gỡ, ghi lại vì không ai đoán được:

- **Dừng `tilt up` xoá theo `loom-api` và `loom-web`.** Ctrl-C không chỉ tắt giao
  diện — nó hạ luôn thứ Tilt đã triển khai. `dex` sống sót vì do `make infra` tạo.
  Muốn giữ app chạy mà không giữ Tilt thì dùng `helm upgrade --install` (xem cuối
  `Tiltfile`), đổi lại mất hot reload.
- **Tilt giữ bốn port trên host:** `8080`, `8000`, `10350` và một port ephemeral. Nếu
  `8080` đang bị chiếm thì `tilt up` hỏng ở bước port-forward, không phải ở bước
  build, và thông báo lỗi không chỉ về phía port.

`make smoke` chạy NGAY sau một lần rollout vẫn phải 9/9 — `preStop` trong chart giữ
pod phục vụ tới khi endpoint được gỡ khỏi Service. Nếu nó hỏng lẻ tẻ sau khi deploy
thì thứ cần xem là hook đó, không phải test.
