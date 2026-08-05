#!/usr/bin/env bash
# Mười một phép kiểm chấp nhận, chạy qua HTTP đúng như người dùng thật — không dùng
# kubectl, nên chạy được với bất kỳ môi trường nào:
#
#     make smoke                              # local
#     make smoke BASE=https://loom.dev.noi-bo # dev
#
# CỐ Ý KHÔNG nằm trong CI: workflow không dựng cụm nên không có gì để gọi tới.
# Đây là bài kiểm chạy với một môi trường ĐANG SỐNG — chạy sau `make dev` ở local,
# và sau mỗi lần ArgoCD đồng bộ ở dev/prod. Giai đoạn 6 sẽ nối nó vào e2e tự động.
#
# Mỗi phép kiểm phải THẬT SỰ đỏ khi thứ nó canh hỏng. Dự án này đã sáu lần gặp
# "một phép kiểm xanh mà không kiểm gì cả", nên đừng thêm phép kiểm nào vào đây
# mà chưa tự tay phá thứ nó canh để xem nó có đỏ không.
set -uo pipefail

BASE="${BASE:-http://loom.localhost}"
USER_LOGIN="${SMOKE_USER:-long@loom.local}"
USER_PASS="${SMOKE_PASS:-password}"

JAR="$(mktemp -d)/cookies"
trap 'rm -rf "$(dirname "$JAR")"' EXIT

# Số phép kiểm MONG ĐỢI, khẳng định ở cuối file. Không có nó, xoá một phép kiểm
# vẫn cho "7/7 đạt" và bản báo cáo trông y như trước.
EXPECTED=11

pass=0; fail=0; skipped=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mHỎNG\033[0m %s\n     %s\n' "$1" "$2"; fail=$((fail+1)); }

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1"; }

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

# Dọn: xoá mềm workspace do smoke tạo. Phép 10 tạo một workspace THẬT trên Aiven mỗi
# lần chạy, nên không dọn thì hai mươi lần chạy để lại hai mươi workspace rác.
# Xoá mềm nên lịch sử audit còn nguyên — và audit của một lần smoke là bằng chứng nó
# đã chạy thật.
if [ -n "$smoke_ws_id" ]; then
  curl -s -b "$JAR" -o /dev/null -X DELETE --max-time 10 \
    "$BASE/api/v1/workspaces/$smoke_ws_id" || true
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
