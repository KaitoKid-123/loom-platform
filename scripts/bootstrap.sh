#!/usr/bin/env bash
# Cài công cụ phát triển vào ~/.local/bin (không cần sudo) và kiểm tra môi trường.
# Chạy được nhiều lần: đã đúng phiên bản thì bỏ qua.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
set -a; . "$ROOT/deploy/versions.env"; set +a

BIN="${LOOM_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN"

have() { command -v "$1" >/dev/null 2>&1; }

# envsubst (gettext-base) nằm trong danh sách vì `make infra` render dex.yaml qua
# nó; thiếu envsubst thì manifest ra rỗng và `kubectl apply` báo lỗi khó hiểu.
for tool in docker kubectl helm uv node npm jq curl tar envsubst getent; do
  have "$tool" || { echo "THIẾU công cụ bắt buộc: $tool" >&2; exit 1; }
done

# Phiên bản đang cài, in ra dạng vX.Y.Z. Trả về rỗng nếu không đọc được.
installed_version() {
  case "$1" in
    k3d)         k3d version 2>/dev/null | awk '/^k3d version/ {print $3; exit}' ;;
    tilt)        tilt version 2>/dev/null | awk '{gsub(/,/,""); print $1; exit}' ;;
    kubeconform) kubeconform -v 2>/dev/null | head -1 ;;
  esac
}

# Cài khi THIẾU, hoặc khi bản đang có LỆCH với pin. Chỉ kiểm tra `command -v` là
# chưa đủ: một bản k3d cũ còn sót lại sẽ khiến K3D_VERSION không bao giờ có tác
# dụng, đúng kiểu "pin mà không ai đọc".
needs_install() {
  local bin="$1" want="$2" cur
  have "$bin" || return 0
  cur="$(installed_version "$bin")"
  [ "$cur" = "$want" ] && return 1
  echo "→ $bin đang là '${cur:-không rõ}', pin là '$want' — cài lại"
  return 0
}

fetch() { curl -sSfL --retry 3 --retry-delay 2 "$@"; }

if needs_install k3d "$K3D_VERSION"; then
  echo "→ cài k3d ${K3D_VERSION}"
  # Tải ra file tạm rồi mới mv: đứt mạng giữa chừng không để lại binary cụt.
  fetch "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-amd64" \
    -o "$BIN/.k3d.tmp"
  chmod +x "$BIN/.k3d.tmp"
  mv "$BIN/.k3d.tmp" "$BIN/k3d"
fi

if needs_install tilt "$TILT_VERSION"; then
  echo "→ cài tilt ${TILT_VERSION}"
  fetch "https://github.com/tilt-dev/tilt/releases/download/${TILT_VERSION}/tilt.${TILT_VERSION#v}.linux.x86_64.tar.gz" \
    | tar -xz -C "$BIN" tilt
fi

if needs_install kubeconform "$KUBECONFORM_VERSION"; then
  echo "→ cài kubeconform ${KUBECONFORM_VERSION}"
  fetch "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" \
    | tar -xz -C "$BIN" kubeconform
fi

docker info >/dev/null 2>&1 || { echo "Docker daemon không chạy." >&2; exit 1; }

# `loom.localhost` phải phân giải về loopback. Hai tầng, vì chúng khác nhau thật:
#
#   - NSS (getent/glibc): thứ MỌI client dùng — Python, Go, psql, kubectl...
#   - curl: từ 7.77 tự phân giải `*.localhost` về loopback theo RFC 6761, kể cả
#     khi NSS không biết tên đó. Trình duyệt Chrome/Firefox cũng vậy.
#
# Nên có máy mà smoke test bằng curl chạy ngon trong khi getent thì bó tay. Hạ
# xuống cảnh báo trong đúng trường hợp đó thay vì chặn cả script: /etc/hosts cần
# sudo, và ta không ép người dùng nâng quyền chỉ để dựng cụm dev.
HOSTS_LINE="echo '127.0.0.1 loom.localhost' | sudo tee -a /etc/hosts"

curl_can_resolve() {
  local rc=0
  # Cổng 9 (discard) chỉ để ép curl phân giải tên rồi thôi, không cần ai lắng nghe.
  curl -s -o /dev/null --connect-timeout 2 "http://$1:9/" >/dev/null 2>&1 || rc=$?
  # 6 = CURLE_COULDNT_RESOLVE_HOST. Mã khác (kể cả 7 "connection refused")
  # nghĩa là tên ĐÃ phân giải được.
  [ "$rc" -ne 6 ]
}

if getent hosts loom.localhost >/dev/null 2>&1; then
  echo "→ loom.localhost phân giải qua NSS: ok"
elif curl_can_resolve loom.localhost; then
  cat >&2 <<EOF

CẢNH BÁO: loom.localhost không có trong NSS (getent không thấy), nhưng curl và
trình duyệt vẫn tự phân giải *.localhost về 127.0.0.1 theo RFC 6761 — nên
'make dev' và scripts/smoke.sh vẫn chạy được.

Client nào dùng getaddrinfo của glibc (Python, Go, psql...) sẽ KHÔNG phân giải
được. Muốn dứt điểm thì chạy một lần:

    $HOSTS_LINE

EOF
else
  cat >&2 <<EOF
loom.localhost không phân giải được bằng cách nào cả. Chạy một lần rồi thử lại:

    $HOSTS_LINE

EOF
  exit 1
fi

available_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
if [ "$available_mb" -lt 2500 ]; then
  cat >&2 <<EOF

CẢNH BÁO: chỉ còn ${available_mb} MB RAM khả dụng, cụm cần khoảng 1600 MB.
Đóng bớt cửa sổ Chrome / VS Code trước khi chạy 'make dev'.

EOF
fi

echo "Bootstrap xong. Nếu '$BIN' chưa nằm trong PATH, thêm vào ~/.bashrc:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
