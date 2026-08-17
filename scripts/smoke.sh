#!/usr/bin/env bash
# Mười lăm phép kiểm chấp nhận, chạy qua HTTP đúng như người dùng thật — không dùng
# kubectl, nên chạy được với bất kỳ môi trường nào:
#
#     make smoke                              # local
#     make smoke BASE=https://loom.dev.noi-bo # dev
#
# CỐ Ý KHÔNG nằm trong CI: workflow không dựng cụm nên không có gì để gọi tới.
# Đây là bài kiểm chạy với một môi trường ĐANG SỐNG — chạy sau `make dev` ở local,
# và sau mỗi lần ArgoCD đồng bộ ở dev/prod. Giai đoạn 6 sẽ nối nó vào e2e tự động.
#
# Mỗi phép kiểm phải THẬT SỰ đỏ khi thứ nó canh hỏng. Dự án này đã mười bốn lần
# gặp "một phép kiểm xanh mà không kiểm gì cả", nên đừng thêm phép kiểm nào vào
# đây mà chưa tự tay phá thứ nó canh để xem nó có đỏ không.
set -uo pipefail

BASE="${BASE:-http://loom.localhost}"
USER_LOGIN="${SMOKE_USER:-long@loom.local}"
USER_PASS="${SMOKE_PASS:-password}"

# NGUỒN của phép 14 — một Postgres THẬT mà cụm với tới được.
#
# Không có mặc định cho host/dbname, và đó là bắt buộc: địa chỉ nguồn ở local
# nằm trong `deploy/local/aiven.env`, một file GITIGNORE chứa credential thật.
# Viết cứng nó vào đây là đưa một phần credential vào một repo công khai. Target
# `make smoke` đọc file đó rồi truyền ba biến này vào (xem Makefile); chạy
# `./scripts/smoke.sh` tay thì tự đặt chúng.
#
# Thiếu chúng, phép 14 báo HỎNG kèm cách sửa — KHÔNG bỏ qua. Một phép kiểm không
# chạy được thì chưa đạt; đó là luật ở đầu file này.
SMOKE_SOURCE_HOST="${SMOKE_SOURCE_HOST:-}"
SMOKE_SOURCE_PORT="${SMOKE_SOURCE_PORT:-5432}"
SMOKE_SOURCE_DB="${SMOKE_SOURCE_DB:-}"
# ĐỊA CHỈ của k8s Secret mang credential nguồn, không phải credential. Khoá bên
# trong Secret phải tên `LOOM_TASK_SOURCE_USER`/`LOOM_TASK_SOURCE_PASSWORD`:
# `JobLauncher.launch` chiếu CẢ Secret vào pod nạp bằng `envFrom`, nên tên khoá
# LÀ tên biến môi trường mà `loom_task.config.SourceCredentials` đọc. Ở local
# Secret này do `make infra-local-source-secret` tạo. `#<key>` chỉ là chú thích
# cho người đọc — `envFrom` không chọn khoá nào (xem `secret_name_for`).
SMOKE_SOURCE_SECRET_REF="${SMOKE_SOURCE_SECRET_REF:-k8s://loom/loom-source-local#LOOM_TASK_SOURCE_PASSWORD}"
# `alembic_version` chứ không một bảng nghiệp vụ nào: nó CHẮC CHẮN tồn tại trong
# database mà migration vừa chạy lên, chỉ có một cột `character varying`, và giữ
# ĐÚNG MỘT dòng (một head duy nhất — `alembic heads` xác nhận). Ba tính chất đó
# làm số dòng khẳng định được mà không cần smoke đụng tới nguồn bằng SQL —
# và smoke KHÔNG có đường nào để đụng: nó chỉ nói HTTP với loom-api.
SMOKE_SOURCE_STREAM="${SMOKE_SOURCE_STREAM:-public.alembic_version}"
SMOKE_SOURCE_ROWS="${SMOKE_SOURCE_ROWS:-1}"

JAR="$(mktemp -d)/cookies"

# Dọn workspace do smoke tạo — Ở TRONG TRAP, không ở cuối file, và phép 15 là lý
# do nó phải chuyển vào đây.
#
# Trước phép 15, để lệnh dọn ở cuối file là đủ: thứ bị bỏ lại khi ai đó bấm
# Ctrl-C giữa chừng chỉ là một workspace rác trong Postgres. Phép 15 tạo một
# pipeline có LỊCH `* * * * *`, tức là một thứ TỰ CHẠY — bỏ lại nó nghĩa là cụm
# phóng một Job nạp mỗi phút, MÃI MÃI, và không ai nhìn `make smoke` mà đoán được
# đó là nguồn.
#
# Dọn HAI lớp, và lớp thứ nhất không thừa. Xoá mềm một WORKSPACE chỉ đặt
# `workspace.state`; nó KHÔNG chạm `item.state` của các item bên trong (không có
# cascade — xem `WorkspaceStore.soft_delete`). Nhịp lịch có lọc `Workspace.state
# == ACTIVE` nên chỉ xoá workspace là ĐỦ để dừng lịch — nhưng vế đó vừa mới được
# thêm vào chính vì phép 15 đo được hậu quả của việc thiếu nó (ba pipeline bỏ
# lại từ ba lần chạy vẫn sinh Job sau khi workspace đã biến mất). Xoá thẳng item
# pipeline là cái phanh KHÔNG phụ thuộc vào vế đó: nó đặt `item.state`, thứ mọi
# phiên bản của nhịp lịch đều lọc. Với một thứ tự phóng pod, hai cái phanh rẻ
# hơn một.
#
# Xoá MỀM cả hai nên lịch sử audit còn nguyên — và audit của một lần smoke là
# bằng chứng nó đã chạy thật. Phép 10 tạo workspace, phép 12 thêm một item
# lakehouse, phép 14 một item connection, phép 15 một item pipeline; cả bốn
# thuộc cùng một workspace đã biến mất.
#
# `${smoke_ws_id:-}` chứ không `$smoke_ws_id`: trap chạy trên MỌI đường thoát, kể
# cả một lần thoát trước khi phép 10 kịp khai biến, và `set -u` sẽ biến chính bộ
# dọn thành lỗi.
#
# CÁI KHÔNG ĐƯỢC DỌN, và hãy đọc kỹ trước khi tin rằng nó tự hết: phép 13 tạo
# bảng Iceberg THẬT (`smoke_ns.ctas_result`), phép 14 một bảng bronze THẬT, phép
# 15 một bảng silver THẬT, cả ba với file Parquet thật trong MinIO. Xoá mềm
# workspace KHÔNG chạm tới chúng — nó chỉ đặt một cột `deleted_at` trong
# Postgres. Warehouse Lakekeeper cũng ở lại (nợ đã ghi ở Giai đoạn 2b), và đo
# thật ở Giai đoạn 2c cho thấy xoá warehouse qua API quản trị của Lakekeeper
# CŨNG KHÔNG xoá object dưới S3 — muốn sạch phải purge S3 tường minh. Nên mỗi
# lần chạy smoke để lại vài bảng một dòng nằm lại vĩnh viễn. Nhỏ, nhưng không có
# giới hạn trên. Dọn chúng cần một đường DROP TABLE mà API truy vấn KHÔNG có:
# `loom_sql.deps` chỉ nhận `CREATE [OR REPLACE] TABLE ... AS SELECT` làm câu
# GHI, còn `DROP TABLE` lọt qua cổng như một câu ĐỌC rồi chết trong DuckDB. Đây
# là nợ có ý thức chứ không phải sơ suất.
# SC2317: shellcheck không lần được lời gọi đi qua `trap`, nên nó coi cả thân
# hàm là mã chết. Tắt đúng một mã, ở đúng một hàm — chứ không tắt cả file.
# shellcheck disable=SC2317
cleanup() {
  # Pipeline TRƯỚC workspace: đây là thứ duy nhất đang tự chạy, nên nó phải dừng
  # kể cả khi lời gọi xoá workspace ngay dưới trượt.
  if [ -n "${smoke_pipeline_id:-}" ]; then
    curl -s -b "$JAR" -o /dev/null -X DELETE --max-time 10 \
      "$BASE/api/v1/items/$smoke_pipeline_id" || true
  fi
  if [ -n "${smoke_ws_id:-}" ]; then
    curl -s -b "$JAR" -o /dev/null -X DELETE --max-time 10 \
      "$BASE/api/v1/workspaces/$smoke_ws_id" || true
  fi
  rm -rf "$(dirname "$JAR")"
}
trap cleanup EXIT

# Số phép kiểm MONG ĐỢI, khẳng định ở cuối file. Không có nó, xoá một phép kiểm
# vẫn cho "7/7 đạt" và bản báo cáo trông y như trước.
EXPECTED=15

pass=0; fail=0; skipped=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mHỎNG\033[0m %s\n     %s\n' "$1" "$2"; fail=$((fail+1)); }

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1"; }

# Trần thời gian chờ một query chạy xong.
#
# Bản đầu không có hằng số này — nó lặp 10 vòng với `sleep 0.3`, tức chưa tới 3
# giây, và con số 3 không đến từ đâu cả. Nó xanh ở Task 8 rồi đỏ ở Task 10 mà
# KHÔNG có dòng mã nào đổi giữa hai lần.
#
# Đo thật lúc đó: CTAS mất **11,3s**, câu SELECT đọc lại mất **7,2s**. Phép đo
# 50 GB ở cùng ngày giải thích vì sao — một lần commit catalog trung bình 6,7s,
# p95 11,2s, max 15,7s, vì Lakekeeper nói chuyện với Postgres trên Aiven qua
# internet. Một CTAS còn phải tạo namespace và ghi Parquet trước khi commit.
#
# Nên 3 giây chưa bao giờ đúng; nó chỉ tình cờ đủ vào một ngày mạng nhanh. Trần
# ở đây là ~4 lần cái đuôi dày nhất đo được, và nó chỉ là TRẦN — vòng lặp thoát
# ngay khi query xong, nên đặt rộng không làm bài kiểm chậm đi khi mọi thứ ổn.
QUERY_TIMEOUT_S="${QUERY_TIMEOUT_S:-60}"

# Chờ một query tới trạng thái CUỐI. In ra "<trạng thái> <số giây đã chờ>".
#
# In cả hai thứ qua stdout, KHÔNG đặt biến toàn cục: chỗ gọi dùng `$(...)`, mà
# lệnh trong `$(...)` chạy ở subshell nên mọi phép gán bên trong biến mất khi nó
# thoát. Bản đầu của hàm này đặt một biến `WAITED_S` rồi thông báo lỗi in ra
# `sau 0s` mãi mãi — đúng cái mà nó tự nhận là đã sửa.
#
# Danh sách trạng thái cuối là ALLOWLIST (`loom_query.store.QueryStatus`), không
# phải "khác running thì thôi". Khác nhau ở chỗ hỏng: khi curl trượt hoặc Traefik
# trả một trang HTML 502, `jq` cho chuỗi RỖNG — mà rỗng cũng "khác running", nên
# bản đầu thoát ngay vòng đầu tiên và vứt cả ngân sách 60 giây. Một cú nghẽn
# mạng trong 11 giây chờ CTAS đủ để làm đỏ cả bài kiểm. Rỗng nghĩa là CHƯA BIẾT,
# và chưa biết thì phải hỏi lại.
wait_for_query() {   # $1 = query_id, $2 = file hứng phản hồi
  local id="$1" out="$2" started="$SECONDS" status=""
  while [ $((SECONDS - started)) -lt "$QUERY_TIMEOUT_S" ]; do
    curl -s -b "$JAR" --max-time 10 -o "$out" "$BASE/api/v1/query/$id"
    status=$(jq -r '.status // empty' < "$out" 2>/dev/null)
    case "$status" in
      succeeded|failed|cancelled) break ;;
    esac
    sleep 0.5
  done
  printf '%s %s' "${status:-không-đọc-được}" "$((SECONDS - started))"
}

# Trần thời gian chờ MỘT LẦN NẠP chạy xong. Một hằng số RIÊNG, không dùng lại
# `QUERY_TIMEOUT_S`, và lý do là chính bài học đã sinh ra `QUERY_TIMEOUT_S`: trần
# phải đến từ số đo của THAO TÁC ĐANG CHỜ. Một lần nạp không phải một câu truy
# vấn — nó còn phải xếp lịch một pod, kéo ảnh (nếu node chưa có), quay số tới
# một Postgres NGOÀI cụm, rồi commit catalog NHIỀU lần (`full` ghi staging, đổi
# tên đích đi, đưa staging lên, bỏ đích cũ).
#
# Đo thật trên cụm này ngày 2026-08-13, bảng một dòng, ảnh đã có sẵn trong node:
# bốn lần chạy cho `DURATION` của Job là 12s, 13s, 14s, 14s, và từ lúc POST tới
# lúc `status=succeeded` là 10,4s. Phần lớn thời gian đó KHÔNG phải đọc dữ liệu —
# một dòng thì đọc gần như tức thì — mà là xếp lịch pod cộng các lần commit
# catalog, thứ mà phép đo 50 GB ở Giai đoạn 2c đo được trung bình 6,7s và max
# 15,7s MỖI LẦN vì Lakekeeper nói chuyện với Postgres trên Aiven qua internet.
#
# 120s là ~8 lần con số đo được, và nó phải rộng thế: một node CHƯA có ảnh
# `loom/task` phải kéo ảnh trước, và bốn lần commit ở đuôi p95 đã là ~45s. Đây
# chỉ là TRẦN — vòng lặp thoát ngay khi run kết thúc, nên rộng không làm bài
# kiểm chậm đi khi mọi thứ ổn.
INGEST_TIMEOUT_S="${INGEST_TIMEOUT_S:-120}"

# Chờ một lần nạp tới trạng thái CUỐI. In ra "<trạng thái> <số giây đã chờ>".
#
# CÙNG hình dạng với `wait_for_query` ở trên, và cố ý cùng: in cả hai thứ qua
# stdout (chỗ gọi đọc bằng `read -r ... < <(...)`, vì `$(...)` chạy ở subshell
# nên mọi phép gán bên trong biến mất), và danh sách trạng thái cuối là
# ALLOWLIST chứ không "khác pending/running thì thôi" — một chuỗi RỖNG (curl
# trượt, Traefik trả HTML 502) cũng "khác running", và bản đầu của `wait_for_
# query` đã vì thế thoát ngay vòng đầu rồi vứt cả ngân sách chờ.
#
# HAI trạng thái cuối, không ba: `ingest_run.status` KHÔNG có `cancelled` (xem
# docstring `IngestRun` ở `models.py` — chưa có gì tự sinh run nên chưa có gì để
# huỷ). Chép thêm `cancelled` vào đây sẽ là một nhánh canh một trạng thái không
# tồn tại.
wait_for_ingest() {   # $1 = run_id, $2 = file hứng phản hồi
  local id="$1" out="$2" started="$SECONDS" status=""
  while [ $((SECONDS - started)) -lt "$INGEST_TIMEOUT_S" ]; do
    curl -s -b "$JAR" --max-time 10 -o "$out" "$BASE/api/v1/ingest/$id"
    status=$(jq -r '.status // empty' < "$out" 2>/dev/null)
    case "$status" in
      succeeded|failed) break ;;
    esac
    sleep 0.5
  done
  printf '%s %s' "${status:-không-đọc-được}" "$((SECONDS - started))"
}

# Chu kỳ gõ nhịp của `loom-scheduler` — `deploy/helm/loom/values.yaml`
# (`scheduler.tickSeconds`), đọc lại ở đây vì smoke không có đường nào hỏi cụm
# giá trị thật. Lệch giá trị này chỉ làm TRẦN dưới đây sai, không làm phép kiểm
# sai: vòng lặp thoát ngay khi run kết thúc.
SCHEDULER_TICK_S="${SCHEDULER_TICK_S:-30}"

# Trần thời gian chờ MỘT PIPELINE ĐƯỢC LẬP LỊCH đi hết chuỗi `ingest → sql`.
#
# Hằng số RIÊNG, và nó KHÔNG phải một con số tròn đoán ra — nó là TỔNG của những
# quãng chờ có thật, mỗi quãng dẫn từ một chỗ đã đo hoặc đã cấu hình:
#
#   60s   một phút cron. Lịch của phép 15 là `* * * * *` và neo của một pipeline
#         chưa từng chạy là `item.updated_at` (xem `_due_at`), nên nhịp đầu tiên
#         rơi vào mốc phút KẾ TIẾP sau lúc smoke tạo item — xa nhất là 60s.
#   30s   một nhịp tick để scheduler NHÌN THẤY mốc đó và tạo hàng `pipeline_run`
#         + khởi động bước 0.
#  120s   ngân sách của chính bước nạp — dùng lại `INGEST_TIMEOUT_S`, con số đã
#         được dẫn từ số đo ở trên (Job 12–14s, đuôi p95 của commit catalog).
#   30s   một nhịp nữa: bước nạp xong KHÔNG báo cho ai — tick phải hỏi lại mới
#         biết (xem `_reconcile_ingest_step`), và cùng nhịp đó mới nộp câu SQL.
#   60s   ngân sách của bước SQL — dùng lại `QUERY_TIMEOUT_S`, đã dẫn từ số đo
#         CTAS 11,3s cộng đuôi commit catalog.
#   30s   nhịp cuối: `loom-query` cũng không gọi lại, nên tick phải hỏi lại mới
#         đóng được run.
#
# Tổng 330s. Sàn quan sát được là ~64s (mốc cron rơi ngay, cộng hai nhịp 30s để
# đi hết hai bước) và điều đó đã đo trên cụm sống; 330 là ~5 lần con số đó, và
# phần dôi ra nằm đúng ở những chỗ đã biết là có đuôi dày.
#
# **Tính bằng công thức chứ không viết cứng, có chủ đích.** File này đã hai lần
# trả giá cho một trần bịa: phép 13 dùng 3 giây cho một thao tác 11,3 giây (xanh
# ở một task, đỏ ở task sau mà không dòng mã nào đổi), và phép 12 dùng 15 giây
# rồi biến một 500 chẩn đoán được thành một `000` mù — ba giả thuyết sai trước
# khi ai đó nhìn thấy lỗi thật. Một trần quá ngắn không chỉ làm hỏng phép kiểm,
# nó GIẤU nguyên nhân. Dẫn từ hai hằng số đã đo nghĩa là nâng một trong hai thì
# trần này tự đi theo, thay vì trôi khỏi nhau trong im lặng.
PIPELINE_TIMEOUT_S="${PIPELINE_TIMEOUT_S:-$((60 + SCHEDULER_TICK_S + INGEST_TIMEOUT_S \
  + SCHEDULER_TICK_S + QUERY_TIMEOUT_S + SCHEDULER_TICK_S))}"

# Chờ MỘT pipeline run tới trạng thái CUỐI. In ra "<trạng thái> <số giây> <run_id>".
#
# CÙNG hình dạng với `wait_for_query`/`wait_for_ingest` ở trên, và cố ý cùng: in
# mọi thứ qua stdout để chỗ gọi đọc bằng `read -r ... < <(...)`. KHÔNG dùng
# `$(...)`: lệnh trong đó chạy ở subshell nên mọi phép gán bên trong biến mất
# khi nó thoát — một lỗi đã sửa một lần trong chính file này, và bản đầu của
# `wait_for_query` in ra `sau 0s` mãi mãi vì nó.
#
# Khác hai hàm kia ở đúng một chỗ: run CHƯA TỒN TẠI lúc bắt đầu chờ. Không có
# `run_id` nào để nhận từ một phản hồi 202 — nhịp lịch mới là thứ tạo ra hàng,
# và nó chưa chạy. Nên vòng lặp có hai thì: hỏi DANH SÁCH cho tới khi có run,
# rồi hỏi CHI TIẾT run đó cho tới khi nó đóng.
#
# `run_id` GHIM lại ở lần đầu nhìn thấy, không hỏi lại danh sách nữa. Cron mỗi
# phút có thể sinh một run THỨ HAI trong lúc ta còn đang chờ run thứ nhất; đọc
# lại `items[0]` sẽ nhảy sang nó và bỏ dở thứ đang theo dõi.
#
# Danh sách trạng thái cuối là ALLOWLIST (`pipeline_run.status`: `pending`,
# `running`, `succeeded`, `failed`, `skipped`), không phải "khác running thì
# thôi" — cùng lý do hai hàm kia ghi: khi curl trượt hoặc Traefik trả một trang
# HTML 502, `jq` cho chuỗi RỖNG, mà rỗng cũng "khác running". Rỗng nghĩa là CHƯA
# BIẾT, và chưa biết thì phải hỏi lại.
#
# `skipped` LÀ một trạng thái cuối: một nhịp bị bỏ chiếm chỗ của nhịp đó và
# không bao giờ được thử lại (xem docstring `routers/internal_schedule.py`). Đợi
# tiếp một run `skipped` là đợi tới hết trần cho một thứ đã kết thúc.
#
# `sleep 2` chứ không `sleep 0.5` như hai hàm kia: thứ đang chờ ở đây tính bằng
# phút và nó chỉ nhúc nhích mỗi 30 giây (một nhịp tick), nên hỏi hai lần một
# giây chỉ là 600 request cho cùng một câu trả lời.
wait_for_pipeline_run() {   # $1 = pipeline_id, $2 = file hứng CHI TIẾT run
  local pipeline="$1" out="$2" started="$SECONDS" status="" run_id=""
  local listing; listing="$(dirname "$out")/pipeline_run_list.json"
  while [ $((SECONDS - started)) -lt "$PIPELINE_TIMEOUT_S" ]; do
    if [ -z "$run_id" ]; then
      curl -s -b "$JAR" --max-time 10 -o "$listing" \
        "$BASE/api/v1/pipelines/$pipeline/runs?limit=1"
      run_id=$(jq -r '.items[0].run_id // empty' < "$listing" 2>/dev/null)
    fi
    if [ -n "$run_id" ]; then
      curl -s -b "$JAR" --max-time 10 -o "$out" "$BASE/api/v1/pipeline-runs/$run_id"
      status=$(jq -r '.status // empty' < "$out" 2>/dev/null)
      case "$status" in
        succeeded|failed|skipped) break ;;
      esac
    fi
    sleep 2
  done
  printf '%s %s %s' "${status:-chưa-có-run}" "$((SECONDS - started))" "${run_id:-KHÔNG-CÓ}"
}

echo "Smoke test: $BASE"

# 1 — web phục vụ được trang gốc
c=$(code "$BASE/")
if [ "$c" = 200 ]; then ok "web phục vụ /"; else bad "web phục vụ /" "mong 200, nhận $c"; fi

# 2 — SPA fallback: đường không tồn tại vẫn phải trả index.html, không phải 404.
#     Nếu hỏng thì F5 trên một route bất kỳ của ứng dụng sẽ ra 404.
c=$(code "$BASE/mot/duong/khong/ton/tai")
if [ "$c" = 200 ]; then ok "SPA fallback"; else bad "SPA fallback" "mong 200, nhận $c"; fi

# 3 — healthz: API sống, không đụng database
b=$(curl -s --max-time 10 "$BASE/api/v1/healthz")
if jq -e '.status == "ok"' >/dev/null 2>&1 <<<"$b"; then
  ok "healthz"
else
  bad "healthz" "nhận: ${b:0:120}"
fi

# 4 — readyz: API nối được database. Kiểm ĐÚNG trường .checks.database, không
#     chỉ .status — một readyz trả ok mà không thật sự hỏi database thì vô nghĩa.
b=$(curl -s --max-time 10 "$BASE/api/v1/readyz")
if jq -e '.checks.database == "ok"' >/dev/null 2>&1 <<<"$b"; then
  ok "readyz — database nối được"
else
  bad "readyz — database nối được" "nhận: ${b:0:120}"
fi

# 5 — Dex phục vụ được discovery qua ingress. Đây là bài kiểm cả chuỗi
#     host → loadbalancer → traefik → ingress → dex.
iss=$(curl -s --max-time 10 "$BASE/dex/.well-known/openid-configuration" | jq -r '.issuer // empty')
if [ -n "$iss" ]; then ok "Dex discovery (issuer: $iss)"; else bad "Dex discovery" "không đọc được issuer"; fi

# 6 — đăng nhập trọn vẹn: login → Dex → callback → /me trả đúng người dùng.
#     Bài kiểm nặng nhất: nó chạm PKCE, đổi code lấy token, xác minh ID token
#     qua JWKS, upsert user và tạo phiên trong database.

# Dex viết URL trong HTML, nên `&` thành `&amp;` và `+` thành `&#43;`. Giải mã
# THIẾU `&#43;` là một lỗi im lặng và khó thấy: dấu `#` còn lại mở một fragment
# URL, nên mọi tham số sau `scope=` không bao giờ được gửi đi, `state` tới server
# rỗng, và Dex trả `error=login_failed` — một thông báo không nói gì về nguyên nhân.
unhtml() { sed 's/&amp;/\&/g; s/&#43;/+/g; s/&#34;/"/g'; }

# Đi từ trang Dex tới form/redirect của MỘT connector cụ thể.
#
# Với hai connector trở lên, Dex chèn một trang CHỌN connector vào giữa, và trang
# đó chỉ có thẻ <a> chứ không có <form> — nên đường cũ (grep thẳng `action=`) trả
# về rỗng và cả phép kiểm đăng nhập bị bỏ qua trong im lặng.
dex_connector_url() {
  printf '%s' "$2" | grep -oE "href=\"/dex/auth/$1[^\"]*\"" | head -1 \
    | sed 's/href="//; s/"$//' | unhtml
}

login_ok=0
auth=$(curl -s -c "$JAR" -o /dev/null -w '%{redirect_url}' --max-time 10 "$BASE/api/v1/auth/login")
if [ -n "$auth" ]; then
  form=$(mktemp)
  curl -s -b "$JAR" -c "$JAR" -L --max-time 10 -o "$form" "$auth"
  act=$(grep -oE 'action="[^"]*"' "$form" | head -1 | sed 's/action="//;s/"$//' | unhtml)
  if [ -z "$act" ]; then
    # Đang ở trang chọn connector — vào connector mật khẩu rồi lấy form ở đó.
    local_url=$(dex_connector_url local "$(cat "$form")")
    if [ -n "$local_url" ]; then
      curl -s -b "$JAR" -c "$JAR" -L --max-time 10 -o "$form" "$BASE$local_url"
      act=$(grep -oE 'action="[^"]*"' "$form" | head -1 | sed 's/action="//;s/"$//' | unhtml)
    fi
  fi
  case "$act" in /*) act="$BASE$act";; esac
  rm -f "$form"
  if [ -n "$act" ]; then
    cb=$(curl -s -b "$JAR" -c "$JAR" -o /dev/null -w '%{redirect_url}' --max-time 10 \
          --data-urlencode "login=$USER_LOGIN" --data-urlencode "password=$USER_PASS" "$act")
    case "$cb" in *approval*) cb=$(curl -s -b "$JAR" -c "$JAR" -o /dev/null -w '%{redirect_url}' --max-time 10 "$cb");; esac
    if [ -n "$cb" ]; then curl -s -b "$JAR" -c "$JAR" -o /dev/null --max-time 10 "$cb"; fi
  fi
fi
me=$(curl -s -b "$JAR" --max-time 10 "$BASE/api/v1/me")
if jq -e --arg e "$USER_LOGIN" '.email == $e' >/dev/null 2>&1 <<<"$me"; then
  ok "đăng nhập trọn vẹn — /me trả $USER_LOGIN"
  login_ok=1
else
  bad "đăng nhập trọn vẹn" "/me trả: ${me:0:120}"
fi

# 7 — không token nào lọt xuống trình duyệt. Đây là lời hứa cốt lõi của kiến
#     trúc BFF: trình duyệt chỉ được giữ một session id mờ đục.
#     Chỉ có nghĩa khi phép kiểm 6 đã đăng nhập được — nếu không thì cookie jar
#     rỗng và phép kiểm này xanh mà chẳng chứng minh điều gì.
if [ "$login_ok" -eq 0 ]; then
  bad "không rò rỉ token" "bỏ qua được — đăng nhập hỏng nên không có gì để kiểm"
elif grep -qiE 'access_token|id_token|refresh_token|eyJ[A-Za-z0-9_-]{10,}' "$JAR"; then
  bad "không rò rỉ token" "tìm thấy chuỗi giống token trong cookie"
elif ! grep -q '^#HttpOnly_' "$JAR"; then
  bad "không rò rỉ token" "cookie phiên KHÔNG có cờ HttpOnly"
else
  ok "không rò rỉ token — chỉ có session id HttpOnly"
fi

# 8 — Loom có YÊU CẦU scope `groups`. OIDC không phát claim tuỳ chọn mà client
#     không xin, nên thiếu scope này thì Dex trả một id_token không có `groups`,
#     `_normalise_groups` nhận None và trả tuple rỗng, và toàn bộ RBAC theo nhóm
#     chết mà không một dòng log nào nói gì. Đó là trạng thái của hệ thống trước
#     task này: mọi test về nhóm dựng Principal bằng tay.
#
#     Tách khỏi phép 9 có mục đích: hai phép cùng đỏ khi ta quên scope, nhưng phép
#     này chỉ vào PHÍA MÌNH. Chỉ 9 đỏ nghĩa là ta xin rồi mà IdP không gửi — hai
#     nguyên nhân khác nhau, hai chỗ sửa khác nhau.
if [ -z "$auth" ]; then
  bad "login xin scope groups" "không lấy được URL chuyển hướng của /auth/login"
else
  case "$auth" in
    *scope=*groups*|*+groups*|*%20groups*) ok "login xin scope groups" ;;
    *) bad "login xin scope groups" "$(printf '%s' "$auth" | grep -oE 'scope=[^&]*')" ;;
  esac
fi

# 9 — Claim `groups` đi ĐƯỢC hết chuỗi: Dex → id_token → verifier → user_session
#     → /me. Đây là điều duy nhất mà chỉ một cụm đang sống chứng minh được, và
#     `staticPasswords` không phát được claim này nên phải đi qua connector mock.
#
#     Khẳng định KHÔNG RỖNG, không chỉ "trường có tồn tại": `groups: []` là chính
#     xác cái mà một hệ thống hỏng trả về, nên một phép kiểm chấp nhận nó thì xanh
#     suốt trong khi tính năng không hoạt động.
case "$BASE" in
  *localhost*)
    mockjar="$(dirname "$JAR")/mockjar"
    ma=$(curl -s -c "$mockjar" -o /dev/null -w '%{redirect_url}' --max-time 10 "$BASE/api/v1/auth/login")
    mpage=$(curl -s -b "$mockjar" -c "$mockjar" -L --max-time 10 "$ma")
    murl=$(dex_connector_url mock-groups "$mpage")
    if [ -z "$murl" ]; then
      bad "claim groups tới được /me" "không có connector mock-groups — xem deploy/infra/dex.yaml"
    else
      curl -s -b "$mockjar" -c "$mockjar" -L -o /dev/null --max-time 10 "$BASE$murl"
      mme=$(curl -s -b "$mockjar" --max-time 10 "$BASE/api/v1/me")
      if jq -e '(.groups // []) | length > 0' >/dev/null 2>&1 <<<"$mme"; then
        ok "claim groups tới được /me ($(jq -rc '.groups' <<<"$mme"))"
      else
        bad "claim groups tới được /me" "nhận: ${mme:0:160}"
      fi
    fi
    ;;
  *)
    # dev/prod dùng IdP thật, không có connector mock. BỎ QUA chứ không tính là
    # đạt: một phép kiểm xanh vì không chạy là đúng thứ file này cấm ở đầu trang.
    printf '  \033[33mBỎ QUA\033[0m claim groups tới được /me\n     %s\n' \
      "cần connector mock — chỉ có ở local"
    skipped=$((skipped+1))
    ;;
esac

# 10 — tạo workspace rồi tạo item trong đó, trên một hệ thống ĐANG CHẠY: cổng quyền
#      cấp tenant, cổng quyền cấp workspace, sinh storage_prefix, hash definition, hàng
#      item_version đầu tiên, và ETag trên phản hồi tạo.
#
#      Phép này KHÔNG phân biệt được cổng quyền nào đang được dùng: tài khoản smoke là
#      admin cấp tenant, nên nó thừa hưởng admin ở mọi workspace và một cổng đòi
#      `workspace_delete` thay vì `item_create` vẫn cho qua. Đã kiểm bằng cách đổi
#      đúng thế và thấy 11/11 vẫn xanh. Việc canh ĐÚNG cổng nào thuộc integration test
#      (`test_a_viewer_cannot_create_an_item`); phép này canh cả chuỗi có chạy thật hay
#      không. Nó thấy được: version đầu khác 1, thiếu ETag trên phản hồi tạo, và mọi mã
#      trạng thái lệch — cả ba đã chứng minh đỏ.
#
#      Cần principal là admin cấp tenant. Nếu không phải thì báo HỎNG kèm cách sửa,
#      KHÔNG bỏ qua im lặng: một phép kiểm không chạy được thì chưa đạt.
tmpdir="$(dirname "$JAR")"
smoke_ws_id=""
smoke_item_id=""

if [ "$login_ok" -eq 0 ]; then
  bad "tạo workspace và item qua API" "bỏ qua được — đăng nhập hỏng"
else
  ws_payload=$(printf '{"name":"smoke-%s","display_name":"Smoke %s"}' "$$" "$$")
  ws_code=$(curl -s -b "$JAR" -o "$tmpdir/ws.json" -w '%{http_code}' --max-time 15 \
            -X POST -H 'Content-Type: application/json' -d "$ws_payload" \
            "$BASE/api/v1/workspaces")
  case "$ws_code" in
    201)
      smoke_ws_id=$(jq -r '.id' < "$tmpdir/ws.json")
      item_payload='{"type":"sql_script","name":"smoke-item","display_name":"Smoke item","definition":{"schema_version":1,"sql":"SELECT 1"}}'
      it_code=$(curl -s -b "$JAR" -D "$tmpdir/item.h" -o "$tmpdir/item.json" -w '%{http_code}' \
                --max-time 15 -X POST -H 'Content-Type: application/json' -d "$item_payload" \
                "$BASE/api/v1/workspaces/$smoke_ws_id/items")
      it_etag=$(grep -i '^etag:' "$tmpdir/item.h" | tr -d '\r' | cut -d' ' -f2-)
      if [ "$it_code" != 201 ]; then
        bad "tạo workspace và item qua API" "tạo item trả $it_code"
      elif ! jq -e '.version == 1' >/dev/null 2>&1 < "$tmpdir/item.json"; then
        bad "tạo workspace và item qua API" "item mới không phải version 1"
      elif [ "$it_etag" != 'W/"1"' ]; then
        # ETag ngay trên phản hồi TẠO: thiếu nó thì client phải GET lại trước khi sửa
        # được thứ mình vừa tạo.
        bad "tạo workspace và item qua API" "ETag trên phản hồi tạo: ${it_etag:-KHÔNG CÓ}"
      else
        smoke_item_id=$(jq -r '.id' < "$tmpdir/item.json")
        ok "tạo workspace và item qua API"
      fi
      ;;
    403|404)
      bad "tạo workspace và item qua API" \
          "principal không phải admin cấp tenant (HTTP $ws_code) — chạy 'make grant-admin EMAIL=$USER_LOGIN' rồi thử lại"
      ;;
    *)
      bad "tạo workspace và item qua API" "tạo workspace trả $ws_code"
      ;;
  esac
fi

# 11 — ETag hoạt động end-to-end: PATCH thiếu If-Match phải là ĐÚNG 428.
#      Không nhận "4xx nào cũng được": 400 nghĩa là header bị hiểu sai, còn 412 nghĩa là
#      server tự đoán một version thay vì đòi client nói ra.
if [ -z "$smoke_item_id" ]; then
  # Rỗng nghĩa là phép 10 hỏng. Báo HỎNG chứ không bỏ qua — bỏ qua thì phép 11 luôn
  # "đạt" mỗi khi phép 10 hỏng, đúng kiểu xanh mà không kiểm gì.
  bad "PATCH thiếu If-Match trả 428" "không có item từ phép 10 để kiểm"
else
  code=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' --max-time 10 \
         -X PATCH -H 'Content-Type: application/json' -d '{"display_name":"X"}' \
         "$BASE/api/v1/items/$smoke_item_id")
  if [ "$code" = 428 ]; then
    ok "PATCH thiếu If-Match trả 428"
  else
    bad "PATCH thiếu If-Match trả 428" "nhận $code"
  fi
fi

# 12 — tạo lakehouse qua API rồi hỏi schema của nó: canh đúng đường Giai đoạn 2b
#      Task 12 dựng — `loom-api` phải gọi Lakekeeper cấp một warehouse THẬT TRƯỚC khi
#      commit hàng item (xem docstring `loom_api.warehouse_provisioning`). Nếu bước
#      cấp phát đó bị bỏ qua hay hỏng âm thầm, item vẫn tạo được — hàng Postgres
#      không biết gì về Lakekeeper — nhưng lakehouse RỖNG, và lỗi đó chỉ lộ ra lúc có
#      người MỞ nó, không lộ lúc tạo.
#
#      Không hỏi Lakekeeper trực tiếp được: nó là ClusterIP, không qua ingress, và
#      smoke không dùng kubectl. Hỏi GIÁN TIẾP qua route Task 2 Giai đoạn 2c dựng —
#      `GET /lakehouses/{id}/schema` chuyển tiếp VÔ ĐIỀU KIỆN sang `loom-query` (xem
#      docstring `routers/query.py`), và `loom-query` mở một catalog Iceberg THẬT của
#      ĐÚNG warehouse này để liệt kê namespace. Warehouse không tồn tại thì bước mở
#      catalog đó hỏng — đã kiểm bằng thực nghiệm trên cụm thật: xoá bước cấp phát rồi
#      gọi route này cho ra một trạng thái KHÁC 200, không phải một cây rỗng.
#
#      Dùng CHUNG workspace với phép 10, không tạo workspace thứ hai chỉ cho phép
#      này. KHÔNG dọn riêng lakehouse: xoá mềm workspace ở cuối file kéo theo cả hàng
#      item này, đúng cách phép 10 đã để lại hàng item sql_script của nó. KHÔNG dọn
#      được warehouse Lakekeeper — hệ thống hôm nay không có API xoá warehouse nào
#      (xem docstring `loom_api.warehouse_provisioning`: "không có hàm xoá nào", một
#      nợ đã ghi nhận ở đó, không phải việc của smoke). Mỗi lần chạy vì vậy để lại
#      đúng MỘT warehouse rỗng — không bảng, không dữ liệu, không lớn dần theo cách
#      đáng lo.
smoke_lakehouse_id=""
if [ -z "$smoke_ws_id" ]; then
  bad "tạo lakehouse qua API — warehouse xuất hiện" "bỏ qua được — phép 10 không có workspace để dùng"
else
  lh_payload=$(printf '{"type":"lakehouse","name":"smoke-lakehouse-%s","display_name":"Smoke lakehouse","definition":{"schema_version":1}}' "$$")
  lh_code=$(curl -s -b "$JAR" -o "$tmpdir/lh.json" -w '%{http_code}' --max-time 15 \
            -X POST -H 'Content-Type: application/json' -d "$lh_payload" \
            "$BASE/api/v1/workspaces/$smoke_ws_id/items")
  if [ "$lh_code" != 201 ]; then
    bad "tạo lakehouse qua API — warehouse xuất hiện" "tạo item type=lakehouse trả $lh_code"
  else
    smoke_lakehouse_id=$(jq -r '.id' < "$tmpdir/lh.json")
    schema_code=$(curl -s -b "$JAR" -o "$tmpdir/schema.json" -w '%{http_code}' --max-time 15 \
                  "$BASE/api/v1/lakehouses/$smoke_lakehouse_id/schema")
    if [ "$schema_code" != 200 ]; then
      bad "tạo lakehouse qua API — warehouse xuất hiện" \
          "GET .../schema trả $schema_code (mong 200) — warehouse có được cấp phát không?"
    elif ! jq -e '.namespaces == []' >/dev/null 2>&1 < "$tmpdir/schema.json"; then
      bad "tạo lakehouse qua API — warehouse xuất hiện" "schema trả: $(cat "$tmpdir/schema.json")"
    else
      ok "tạo lakehouse qua API — warehouse xuất hiện (schema rỗng, đúng lakehouse mới)"
    fi
  fi
fi

# 13 — CTAS đi hết đường: trình duyệt -> loom-api -> bí mật chia sẻ -> cổng quyền
#      -> runner -> Lakekeeper THẬT, và ngược lại — rồi SELECT lại đúng dòng vừa
#      tạo. Đây CHÍNH XÁC là tiêu chí nghiệm thu của Giai đoạn 2 quyết định #4:
#      "Tạo được bảng Iceberg bằng CTAS trong SQL editor, thấy nó trong Lakehouse
#      Explorer".
#
#      TRƯỚC bản sửa CTAS (`loom_sql.deps.dependencies` tách đọc/ghi,
#      `loom_query.runner` tự COMMIT kết quả SELECT ra Iceberg qua
#      `Lakehouse.create_from`), phép này CHỈ kiểm được bằng một SELECT nhắm vào
#      bảng không tồn tại — CTAS luôn hỏng với "table not found" TRƯỚC KHI câu
#      CREATE kịp chạy (`dependencies()` cũ coi đích CTAS là một bảng cần ĐỌC, y
#      hệt bảng nguồn). Giờ CTAS chạy được, phép này mạnh lên đúng như spec đòi:
#      KHẲNG ĐỊNH thật, không chỉ TỪ CHỐI.
#
#      `SELECT 1 AS id` (không `FROM` gì): lakehouse vừa cấp phát ở phép 12 RỖNG
#      (`namespaces == []`), nên CTAS ở đây không cần một bảng nguồn có sẵn nào —
#      tự đủ để chứng minh đường "SELECT -> Arrow -> Lakehouse.create_from()".
#      Namespace đích (`smoke_ns`) chưa tồn tại — `runner` phải tự tạo nó
#      (`create_namespace_if_not_exists`) trước khi commit, không phải việc của
#      smoke.
#
#      Tài khoản smoke là admin cấp TENANT (xem phép 10) — thừa hưởng
#      `contributor` trở lên trên MỌI lakehouse trong tenant qua chuỗi tổ tiên
#      RBAC (`loom_api.permissions`), nên CTAS ở đây chạy được mà không cần cấp
#      quyền riêng cho lakehouse vừa tạo.
if [ -z "$smoke_lakehouse_id" ]; then
  bad "CTAS qua /api/v1/query — tạo bảng rồi đọc lại" "không có lakehouse từ phép 12 để kiểm"
else
  ctas_payload=$(printf '{"lakehouse_id":"%s","sql":"CREATE TABLE smoke_ns.ctas_result AS SELECT 1 AS id"}' "$smoke_lakehouse_id")
  ctas_code=$(curl -s -b "$JAR" -o "$tmpdir/ctas.json" -w '%{http_code}' --max-time 15 \
              -X POST -H 'Content-Type: application/json' -d "$ctas_payload" \
              "$BASE/api/v1/query")
  if [ "$ctas_code" != 202 ]; then
    bad "CTAS qua /api/v1/query — tạo bảng rồi đọc lại" \
        "nộp CTAS trả $ctas_code (mong 202) — proxy hoặc bí mật chia sẻ hỏng?"
  else
    ctas_query_id=$(jq -r '.query_id' < "$tmpdir/ctas.json")
    read -r ctas_status ctas_waited_s < <(wait_for_query "$ctas_query_id" "$tmpdir/ctas_status.json")
    if [ "$ctas_status" != succeeded ]; then
      bad "CTAS qua /api/v1/query — tạo bảng rồi đọc lại" \
          "sau ${ctas_waited_s}s (trần ${QUERY_TIMEOUT_S}s) trạng thái cuối: $(cat "$tmpdir/ctas_status.json")"
    else
      sel_payload=$(printf '{"lakehouse_id":"%s","sql":"SELECT id FROM smoke_ns.ctas_result"}' "$smoke_lakehouse_id")
      sel_code=$(curl -s -b "$JAR" -o "$tmpdir/sel.json" -w '%{http_code}' --max-time 15 \
                 -X POST -H 'Content-Type: application/json' -d "$sel_payload" \
                 "$BASE/api/v1/query")
      if [ "$sel_code" != 202 ]; then
        bad "CTAS qua /api/v1/query — tạo bảng rồi đọc lại" \
            "SELECT lại từ bảng vừa tạo trả $sel_code (mong 202)"
      else
        sel_query_id=$(jq -r '.query_id' < "$tmpdir/sel.json")
        read -r sel_status sel_waited_s < <(wait_for_query "$sel_query_id" "$tmpdir/sel_status.json")
        if [ "$sel_status" = succeeded ] && jq -e '.rows == [[1]]' >/dev/null 2>&1 < "$tmpdir/sel_status.json"; then
          ok "CTAS qua /api/v1/query — bảng Iceberg tạo được qua SQL editor, đọc lại đúng dòng"
        else
          bad "CTAS qua /api/v1/query — tạo bảng rồi đọc lại" \
              "SELECT lại sau ${sel_waited_s}s (trần ${QUERY_TIMEOUT_S}s): $(cat "$tmpdir/sel_status.json")"
        fi
      fi
    fi
  fi
fi

# 14 — NẠP đi hết đường, và đây là phép kiểm duy nhất chạm cả BA mặt phẳng:
#      trình duyệt -> loom-api -> Kubernetes (một `Job` thật) -> pod nạp -> Postgres
#      NGUỒN -> Iceberg/Lakekeeper -> ngược về `/internal/ingest/*` -> Postgres control
#      plane, rồi đọc lại qua loom-query. Không một integration test nào dựng nổi
#      chuỗi đó: mọi bài test của Giai đoạn 3a hoặc thay Kubernetes bằng một double
#      (`tests/integration/test_ingest_api.py`) hoặc gọi thẳng `run_full` mà bỏ qua
#      cả `loom-api` lẫn Job (`services/loom-task/tests/integration`).
#
#      Bốn thứ CHỈ phép này chứng minh được, và cả bốn đã hỏng thật ít nhất một lần
#      trong lúc dựng nó: (1) `LOOM_TASK_IMAGE` trỏ vào một tag CÓ trong node;
#      (2) Secret nguồn tồn tại và mang đúng tên khoá mà `SourceCredentials` đọc;
#      (3) Role của `loom-api` đủ quyền cho CẢ `launch` lẫn `status` — thiếu
#      `jobs/status` từng làm `GET /ingest/{id}` trả 500 suốt lúc run đang chạy;
#      (4) bí mật chia sẻ của đường nạp khớp hai đầu, nếu không mọi `/progress`
#      401 và run kẹt ở `running`.
#
#      `mode=full` chứ không `incremental`: `full` không cần cột cursor nào, nên
#      nó chạy được trên MỌI bảng — kể cả `alembic_version`, bảng một cột kiểu
#      `character varying` mà `CURSOR_TYPE_ALLOWLIST` cố ý không nhận. Đổi sang
#      `incremental` ở đây sẽ đỏ với `CursorNotAvailable`, và đó là hành vi đúng.
#
#      Dùng CHUNG workspace và lakehouse với phép 10/12. KHÔNG dọn bảng bronze —
#      cùng món nợ mà phép 13 đã ghi ở khối dọn dẹp bên dưới.
#
# `smoke_conn_name`/`smoke_conn_id` khai TRƯỚC khối, không bên trong: phép 15
# dùng lại đúng connection này (và đúng bảng bronze mà nó nạp vào), nên nó phải
# đọc được hai biến đó kể cả trên những nhánh mà phép 14 thoát sớm — dưới
# `set -u`, một biến chưa từng được gán là một lỗi chứ không phải chuỗi rỗng.
smoke_conn_name=""
smoke_conn_id=""
if [ -z "$smoke_lakehouse_id" ]; then
  bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" "không có lakehouse từ phép 12 để nạp vào"
elif [ -z "$SMOKE_SOURCE_HOST" ] || [ -z "$SMOKE_SOURCE_DB" ]; then
  bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
      "chưa khai nguồn: cần SMOKE_SOURCE_HOST và SMOKE_SOURCE_DB ('make smoke' tự lấy từ deploy/local/aiven.env)"
else
  # Tên connection đi thẳng vào TÊN BẢNG bronze (`bronze.<slug>__<schema>_<bảng>`,
  # xem `loom_task.runner.bronze_table_name`), nên nó phải khớp `ItemCreate.name`
  # (`^[a-z0-9][a-z0-9-]*$`) và KHÔNG được sinh ra hai gạch dưới liền nhau sau khi
  # `-` đổi thành `_` — `$$` là PID nên chỉ gồm chữ số, an toàn với cả hai luật.
  smoke_conn_name="smoke-src-$$"  # xem khối khai báo ngay trên `if`
  conn_payload=$(jq -nc \
    --arg name "$smoke_conn_name" \
    --arg host "$SMOKE_SOURCE_HOST" \
    --argjson port "$SMOKE_SOURCE_PORT" \
    --arg db "$SMOKE_SOURCE_DB" \
    --arg ref "$SMOKE_SOURCE_SECRET_REF" \
    '{type:"connection", name:$name, display_name:"Smoke source",
      definition:{schema_version:1, kind:"postgres", host:$host, port:$port,
                  database:$db, secret_ref:$ref}}')
  conn_code=$(curl -s -b "$JAR" -o "$tmpdir/conn.json" -w '%{http_code}' --max-time 15 \
              -X POST -H 'Content-Type: application/json' -d "$conn_payload" \
              "$BASE/api/v1/workspaces/$smoke_ws_id/items")
  if [ "$conn_code" != 201 ]; then
    bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" "tạo item type=connection trả $conn_code"
  else
    smoke_conn_id=$(jq -r '.id' < "$tmpdir/conn.json")
    ing_payload=$(jq -nc --arg c "$smoke_conn_id" --arg s "$SMOKE_SOURCE_STREAM" \
                  '{connection_id:$c, stream:$s, mode:"full"}')
    ing_code=$(curl -s -b "$JAR" -o "$tmpdir/ingest.json" -w '%{http_code}' --max-time 15 \
               -X POST -H 'Content-Type: application/json' -d "$ing_payload" \
               "$BASE/api/v1/lakehouses/$smoke_lakehouse_id/ingest")
    if [ "$ing_code" != 202 ]; then
      # 400 ở đây gần như luôn là `secret_ref` trỏ sang namespace khác namespace
      # của Job (`envFrom` không vượt được namespace) — nói ra vì thân phản hồi
      # có câu giải thích và người đọc nên nhìn vào nó.
      bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
          "POST .../ingest trả $ing_code (mong 202): $(cat "$tmpdir/ingest.json")"
    else
      ing_run_id=$(jq -r '.run_id' < "$tmpdir/ingest.json")
      read -r ing_status ing_waited_s < <(wait_for_ingest "$ing_run_id" "$tmpdir/ingest_status.json")
      if [ "$ing_status" != succeeded ]; then
        # In cả `error` của hàng run: pod đã bị dọn khi có người đọc tới đây, và
        # cột đó là thứ duy nhất còn lại nói được vì sao (xem `failure_from_job`).
        bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
            "sau ${ing_waited_s}s (trần ${INGEST_TIMEOUT_S}s) run ở: $(cat "$tmpdir/ingest_status.json")"
      else
        # Tên bảng bronze dựng lại TỪ CÙNG hai mảnh mà pod nạp dùng
        # (`bronze_table_name`): slug connection với `-` thành `_`, hai gạch dưới
        # ngăn cách, rồi `schema.table` với `.` thành `_`. Viết lại quy ước ở đây
        # là chấp nhận được vì nó KHÔNG trôi được trong im lặng: lệch một ký tự
        # thì câu SELECT dưới đây hỏng với "table not found" và phép kiểm đỏ.
        bronze_table="bronze.${smoke_conn_name//-/_}__${SMOKE_SOURCE_STREAM//./_}"
        sel2_payload=$(jq -nc --arg lh "$smoke_lakehouse_id" --arg t "$bronze_table" \
                       '{lakehouse_id:$lh, sql:("SELECT count(*) AS n FROM " + $t)}')
        sel2_code=$(curl -s -b "$JAR" -o "$tmpdir/bronze.json" -w '%{http_code}' --max-time 15 \
                    -X POST -H 'Content-Type: application/json' -d "$sel2_payload" \
                    "$BASE/api/v1/query")
        if [ "$sel2_code" != 202 ]; then
          bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
              "SELECT từ $bronze_table trả $sel2_code (mong 202)"
        else
          sel2_query_id=$(jq -r '.query_id' < "$tmpdir/bronze.json")
          read -r sel2_status sel2_waited_s < <(wait_for_query "$sel2_query_id" "$tmpdir/bronze_status.json")
          # HAI khẳng định, không một. Số dòng ĐỌC LẠI ĐƯỢC chứng minh dữ liệu
          # thật sự nằm trong Iceberg; `rows_written` của hàng run chứng minh
          # đường `/progress` đã chạy. Chỉ kiểm cái đầu thì một pod ghi được
          # nhưng không báo về nổi (mọi `/progress` 401) vẫn cho xanh — và đó
          # đúng là hình dạng của một bí mật chia sẻ lệch hai đầu.
          reported=$(jq -r '.rows_written // empty' < "$tmpdir/ingest_status.json")
          if [ "$sel2_status" != succeeded ]; then
            bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
                "SELECT lại sau ${sel2_waited_s}s (trần ${QUERY_TIMEOUT_S}s): $(cat "$tmpdir/bronze_status.json")"
          elif ! jq -e --argjson n "$SMOKE_SOURCE_ROWS" '.rows == [[$n]]' \
                 >/dev/null 2>&1 < "$tmpdir/bronze_status.json"; then
            bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
                "mong $SMOKE_SOURCE_ROWS dòng trong $bronze_table, đọc lại: $(cat "$tmpdir/bronze_status.json")"
          elif [ "$reported" != "$SMOKE_SOURCE_ROWS" ]; then
            bad "nạp qua /ingest — Job chạy rồi đọc lại bronze" \
                "bảng bronze đúng $SMOKE_SOURCE_ROWS dòng nhưng run báo rows_written=${reported:-KHÔNG CÓ} — đường /progress hỏng?"
          else
            ok "nạp qua /ingest — Job nạp $SMOKE_SOURCE_ROWS dòng vào $bronze_table sau ${ing_waited_s}s, đọc lại đúng"
          fi
        fi
      fi
    fi
  fi
fi

# 15 — PIPELINE ĐƯỢC LẬP LỊCH đi hết chuỗi, không ai bấm nút nào. Đây là phép
#      kiểm duy nhất chứng minh đường Giai đoạn 3b tồn tại: một `loom-scheduler`
#      ĐANG GÕ NHỊP -> `POST /internal/schedule/tick` -> nhịp cron tới hạn ->
#      hàng `pipeline_run` -> bước `ingest` (một Job k8s THẬT) -> đối chiếu ->
#      bước `sql` (nộp sang `loom-query` dưới danh nghĩa `run_as`) -> đối chiếu
#      -> run `succeeded` -> bảng silver đọc lại được. Sáu thứ chỉ phép này thấy:
#      (1) pod scheduler còn sống và bí mật chia sẻ của nó khớp hai đầu — nếu
#      không, mọi tick 401 và không nhịp nào tới hạn; (2) lịch được ĐỌC TỪ
#      `item.definition` chứ không từ bảng `pipeline` mà migration 0009 đã bỏ;
#      (3) `run_as_user_id` còn đủ quyền ghi vào lakehouse; (4) `loom-query`
#      chấp nhận principal `run_as` mà tick chuyển tới (integration test dùng
#      một `loom-query` GIẢ nên nó cố ý không chứng minh được điều này — xem
#      docstring `test_internal_schedule.py`); (5) tick đẩy chuỗi qua HAI bước
#      chứ không đứng ở bước 0; (6) hai đường đọc mới của Task 12 trả đúng thứ
#      cần đọc.
#
#      **CHỜ NHỊP CRON THẬT, KHÔNG tự POST tick.** Đây không phải một lựa chọn
#      về khẩu vị: `/internal/schedule/tick` KHÔNG có đường vào từ ngoài cụm.
#      Ingress chỉ định tuyến `/api` (tới loom-api) và `/` (tới web) — xem
#      `deploy/helm/loom/templates/ingress.yaml`, và
#      `test_internal_route_boundary.py` canh đúng bất biến đó. Smoke chỉ nói
#      HTTP qua ingress (không `kubectl`, xem đầu file), và nó cũng không có
#      `X-Loom-Schedule-Secret` — giá trị đó là một k8s Secret. Nên hai cách duy
#      nhất để tự gọi tick là phá ranh giới ingress hoặc dùng kubectl, và cả hai
#      đắt hơn hẳn cái giá phải trả: chờ lâu hơn. Đổi lại, phép kiểm bao luôn
#      chính `loom-scheduler` — thành phần mà không bài test nào khác chạm tới.
#
#      **`CREATE OR REPLACE TABLE`, và đó là thứ `loom-query` THẬT SỰ nhận.** Đã
#      tra chứ không đoán: `loom_sql.deps._create_table_info` đọc
#      `tree.args["replace"]`, `write_target` mang nó theo, và
#      `Lakehouse.create_from(..., replace=True)` bỏ bảng cũ rồi tạo lại. Ba thứ
#      KHÔNG dùng được: `DROP TABLE` (lọt cổng như một câu ĐỌC rồi chết trong
#      DuckDB — không có đường xoá bảng nào ở tầng này), hai câu lệnh cách nhau
#      bằng `;` (400 ở `loom_sql.validate`), và `CREATE TABLE IF NOT EXISTS` (cú
#      pháp qua được nhưng `IF NOT EXISTS` bị BỎ QUA, nên nó hỏng y như `CREATE
#      TABLE` trần). Lịch ở đây là `* * * * *`, nên nhịp thứ hai tới trong lúc
#      smoke còn chưa dọn xong: với một câu `CREATE TABLE` trần, nhịp đó chết
#      với `EntityAlreadyExists` — đã thấy thật trên cụm. `OR REPLACE` cũng là
#      thứ đúng cho một pipeline chạy theo lịch nói chung: một bước dựng silver
#      phải chạy được mỗi đêm.
#
#      Dùng LẠI connection và bảng bronze của phép 14, có chủ đích: bước nạp của
#      pipeline ghi vào ĐÚNG bảng bronze mà phép 14 vừa khẳng định số dòng, nên
#      khi bảng silver ra đúng số dòng đó thì cả hai bước đều đã thật sự chạy.
#      Một lakehouse thứ hai chỉ thêm một warehouse rác mỗi lần chạy.
if [ -z "$smoke_lakehouse_id" ] || [ -z "$smoke_conn_id" ]; then
  bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
      "không có lakehouse/connection từ phép 12/14 để dựng pipeline"
else
  # `run_as_user_id` là BẮT BUỘC cho một lịch đã bật (`ScheduleDefinition
  # ._enabled_names_its_principal`), và nó phải là một `app_user.id` THẬT — cột
  # đó có khoá ngoại. `/api/v1/me` cố ý KHÔNG trả id (nó không chạm database),
  # nên smoke lấy id của chính mình từ audit: mọi hàng audit trong workspace này
  # đều do chính tài khoản smoke sinh ra vài giây trước, ở phép 10.
  smoke_user_id=$(curl -s -b "$JAR" --max-time 10 \
                  "$BASE/api/v1/workspaces/$smoke_ws_id/audit?limit=1" \
                  | jq -r '.items[0].actor_user_id // empty')
  # Dựng lại tên bảng bronze TỪ CÙNG hai mảnh mà pod nạp dùng — cùng quy ước và
  # cùng lý do đã ghi ở phép 14: lệch một ký tự thì bước SQL hỏng với "table not
  # found" và phép kiểm đỏ, nên nó không trôi được trong im lặng.
  pipe_bronze="bronze.${smoke_conn_name//-/_}__${SMOKE_SOURCE_STREAM//./_}"
  pipe_silver="silver.smoke_pipeline"
  if [ -z "$smoke_user_id" ]; then
    bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
        "không đọc được actor_user_id từ audit của workspace — lịch cần run_as_user_id thật"
  else
    pipe_payload=$(jq -nc \
      --arg name "smoke-pipeline-$$" \
      --arg lh "$smoke_lakehouse_id" \
      --arg conn "$smoke_conn_id" \
      --arg stream "$SMOKE_SOURCE_STREAM" \
      --arg sql "CREATE OR REPLACE TABLE $pipe_silver AS SELECT * FROM $pipe_bronze" \
      --arg run_as "$smoke_user_id" \
      '{type:"pipeline", name:$name, display_name:"Smoke pipeline",
        definition:{schema_version:1,
                    steps:[{type:"ingest",
                            ingest:{lakehouse_id:$lh, connection_id:$conn,
                                    stream:$stream, mode:"full"}},
                           {type:"sql",
                            sql:{lakehouse_id:$lh, sql:$sql}}],
                    schedule:{enabled:true, cron:"* * * * *", timezone:"UTC",
                              run_as_user_id:$run_as}}}')
    # `--max-time 30` cho một lời gọi chỉ ghi Postgres (item `pipeline` KHÔNG
    # cấp warehouse nào, khác `lakehouse` ở phép 12). Rộng có chủ đích: bài học
    # của phép 12 là một trần quá chặt biến một 500 ĐỌC ĐƯỢC thành một `000` mù,
    # và ba giả thuyết sai đi sau nó.
    pipe_code=$(curl -s -b "$JAR" -o "$tmpdir/pipeline.json" -w '%{http_code}' --max-time 30 \
                -X POST -H 'Content-Type: application/json' -d "$pipe_payload" \
                "$BASE/api/v1/workspaces/$smoke_ws_id/items")
    if [ "$pipe_code" != 201 ]; then
      bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
          "tạo item type=pipeline trả $pipe_code: $(cat "$tmpdir/pipeline.json")"
    else
      smoke_pipeline_id=$(jq -r '.id' < "$tmpdir/pipeline.json")
      read -r pipe_status pipe_waited_s pipe_run_id \
        < <(wait_for_pipeline_run "$smoke_pipeline_id" "$tmpdir/pipeline_run.json")
      if [ "$pipe_status" != succeeded ]; then
        # In CẢ chi tiết run: `steps[].error` là thứ duy nhất nói được nó chết ở
        # bước nào và vì sao — bước nạp chép nguyên văn lý do của hàng
        # `ingest_run`, bước SQL chép nguyên văn lý do của `loom-query`. In số
        # giây ĐÃ CHỜ chứ không chỉ trần: hai con số cạnh nhau phân biệt "hết
        # giờ" với "hỏng ngay lập tức", và đó là khác biệt giữa hai nguyên nhân
        # hoàn toàn khác nhau.
        bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
            "sau ${pipe_waited_s}s (trần ${PIPELINE_TIMEOUT_S}s) run ${pipe_run_id} ở '${pipe_status}': $(cat "$tmpdir/pipeline_run.json" 2>/dev/null)"
      else
        sel3_payload=$(jq -nc --arg lh "$smoke_lakehouse_id" --arg t "$pipe_silver" \
                       '{lakehouse_id:$lh, sql:("SELECT count(*) AS n FROM " + $t)}')
        sel3_code=$(curl -s -b "$JAR" -o "$tmpdir/silver.json" -w '%{http_code}' --max-time 15 \
                    -X POST -H 'Content-Type: application/json' -d "$sel3_payload" \
                    "$BASE/api/v1/query")
        if [ "$sel3_code" != 202 ]; then
          bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
              "SELECT từ $pipe_silver trả $sel3_code (mong 202)"
        else
          sel3_query_id=$(jq -r '.query_id' < "$tmpdir/silver.json")
          read -r sel3_status sel3_waited_s < <(wait_for_query "$sel3_query_id" "$tmpdir/silver_status.json")
          # HAI khẳng định, không một. Số dòng trong silver chứng minh bước SQL
          # đã ghi thật; `steps[]` đủ hai bước ĐỀU `succeeded` chứng minh chuỗi
          # đi hết chứ không nhảy cóc. Chỉ kiểm cái đầu thì một bảng silver còn
          # sót từ nhịp trước vẫn cho xanh.
          steps_ok=$(jq -r '[.steps[] | select(.status == "succeeded")] | length' \
                     < "$tmpdir/pipeline_run.json" 2>/dev/null)
          if [ "$sel3_status" != succeeded ]; then
            bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
                "SELECT lại sau ${sel3_waited_s}s (trần ${QUERY_TIMEOUT_S}s): $(cat "$tmpdir/silver_status.json")"
          elif ! jq -e --argjson n "$SMOKE_SOURCE_ROWS" '.rows == [[$n]]' \
                 >/dev/null 2>&1 < "$tmpdir/silver_status.json"; then
            bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
                "mong $SMOKE_SOURCE_ROWS dòng trong $pipe_silver, đọc lại: $(cat "$tmpdir/silver_status.json")"
          elif [ "${steps_ok:-0}" != 2 ]; then
            bad "pipeline theo lịch — scheduler chạy hết chuỗi ingest→sql" \
                "run succeeded nhưng chỉ ${steps_ok:-0}/2 bước succeeded: $(cat "$tmpdir/pipeline_run.json")"
          else
            ok "pipeline theo lịch — scheduler đẩy ingest→sql hết chuỗi sau ${pipe_waited_s}s, $pipe_silver đúng $SMOKE_SOURCE_ROWS dòng"
          fi
        fi
      fi
    fi
  fi
fi

echo
total=$((pass + fail + skipped))
if [ "$total" -ne "$EXPECTED" ]; then
  printf '  \033[31mHỎNG\033[0m chạy %d phép kiểm, mong %d — có phép kiểm nào biến mất?\n' \
    "$total" "$EXPECTED"
  exit 1
fi
if [ "$fail" -eq 0 ]; then
  if [ "$skipped" -gt 0 ]; then
    echo "  $pass/$EXPECTED đạt, $skipped bỏ qua."
  else
    echo "  $pass/$EXPECTED đạt."
  fi
  exit 0
fi
echo "  $pass đạt, $fail HỎNG."
exit 1
