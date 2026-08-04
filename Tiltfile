# -*- mode: Python -*-
"""Vòng lặp phát triển local. Dùng đúng Helm chart mà production dùng."""

EXPECTED_CONTEXT = 'k3d-loom'
K3D_CLUSTER = 'loom'
# k3d đặt tên container node theo quy tắc cố định. Cần tên này để KIỂM CHỨNG
# image đã thật sự nằm trong containerd của node (xem build_and_import).
K3D_NODE = 'k3d-%s-server-0' % K3D_CLUSTER

# Cùng lý do tồn tại như target `check-context` trong Makefile: máy này có sẵn
# một kubeconfig context trỏ vào cụm THẬT. Tilt đi thẳng tới kubectl, không qua
# make, nên nó phải tự dựng lại hàng rào đó. `fail()` dừng lúc đọc Tiltfile —
# trước khi bất kỳ manifest nào được apply.
if k8s_context() != EXPECTED_CONTEXT:
    fail('Tilt chỉ chạy trên %s (context hiện tại: %s). Chạy `make cluster-up` trước.'
         % (EXPECTED_CONTEXT, k8s_context()))

# Lớp chặn thứ hai, của chính Tilt. Không thừa: `k8s_context()` chỉ được đánh
# giá một lần lúc load Tiltfile, còn allow_k8s_contexts áp cho mọi lần apply
# về sau.
allow_k8s_contexts(EXPECTED_CONTEXT)

# Rác build không được phép làm bẩn image hay kích hoạt build lại. `tests` nằm
# trong danh sách vì .dockerignore đã loại chúng khỏi build context: sửa một
# file test mà không ignore ở đây thì Tilt build lại ~30 giây để cho ra một
# image giống hệt cũ.
BUILD_IGNORE = [
    '**/__pycache__',
    '**/.pytest_cache',
    '**/.mypy_cache',
    '**/.ruff_cache',
    '**/tests',
]


def build_and_import(dockerfile):
    """Build image rồi nạp vào node k3d, và kiểm chứng là nó thật sự tới nơi.

    Starlark KHÔNG nối hai string literal đặt cạnh nhau như Python — 'a' 'b' là
    lỗi cú pháp ("got string literal, want ','"). Phải nối bằng '+' tường minh.

    Ba chi tiết dưới đây đều là hậu quả của lỗi đã gặp thật, đừng gỡ:

    1. `flock` — `k3d image import` KHÔNG an toàn khi chạy song song. Tilt build
       api và web đồng thời; hai tiến trình k3d khởi động trong cùng một giây
       sẽ sinh ra CÙNG một đường dẫn tarball trong volume image dùng chung
       (`/k3d/images/k3d-<cluster>-images-<YYYYMMDDhhmmss>.tar`). Cái sau đè
       cái trước, rồi CẢ HAI import cái còn sống và CẢ HAI báo "Successfully
       imported". Đã quan sát trực tiếp: image web vào node ba lần, image api
       không lần nào, exit code 0 cả hai.

    2. `-m direct` — bỏ hẳn tools-node và volume dùng chung (nguồn gốc của (1)),
       đồng thời tiết kiệm ~4s mỗi lần build vì không phải dựng rồi xoá
       k3d-tools.

    3. `crictl inspecti` — vì (1) đã cho thấy k3d có thể báo thành công mà
       không nạp gì cả, exit code của nó là không đủ để tin. Lệnh này trả 1 khi
       thiếu image nên nó thật sự kiểm tra được điều gì đó.
       (`crictl images -q <ref>` thì KHÔNG: đối số lọc bị bỏ qua, nó liệt kê
       mọi image và luôn trả 0 — một cái check không check gì hết.)
    """
    # Mỗi bước tự thoát bằng lỗi của chính nó. Viết liền một dây
    # `a && b && c || { echo ...; }` thì lệnh build hỏng cũng in ra thông báo
    # về import — sai chỗ, và đi tìm nhầm hướng.
    return '\n'.join([
        'set -e',
        'docker build -f %s -t $EXPECTED_REF .' % dockerfile,
        'flock /tmp/loom-k3d-image-import.lock' +
        ' k3d image import $EXPECTED_REF -c %s -m direct' % K3D_CLUSTER,
        'docker exec %s crictl inspecti -q docker.io/$EXPECTED_REF >/dev/null || {' % K3D_NODE,
        '  echo "k3d báo import xong nhưng $EXPECTED_REF KHÔNG có trong %s' % K3D_NODE +
        ' — xem ghi chú (1) trong Tiltfile" >&2',
        '  exit 1',
        '}',
    ])


# HAI CỜ skips_local_docker / disable_push ĐI CÙNG NHAU, thiếu một là hỏng —
# đã gặp thật cả hai:
#
# disable_push: không có nó, Tilt coi bước "đưa image tới cụm" là `docker push`
#   và bắn thẳng lên docker.io/loom/* — "push access denied, repository does
#   not exist". Tilt chỉ tự bỏ qua push khi daemon docker CHÍNH LÀ runtime của
#   cụm; k3d không phải vậy, và cơ chế nạp-image sẵn có của Tilt cho kind/k3d
#   chỉ áp dụng cho docker_build.
#
# skips_local_docker: Tilt truyền cho lệnh build một tag TẠM
#   `loom/api:tilt-build-<timestamp>` qua $EXPECTED_REF. Với
#   skips_local_docker=False, sau đó Tilt đọc image từ daemon docker, băm nội
#   dung và triển khai một tag KHÁC — `loom/api:tilt-<hash>`. Nhưng
#   `k3d image import` ở trên đã nạp vào node theo tag TẠM, nên node không hề
#   có tag đã triển khai → ImagePullBackOff, kubelet quay ra hỏi docker.io.
#   Đặt True thì Tilt triển khai đúng $EXPECTED_REF — cùng tag vừa nạp.
custom_build(
    'loom/api',
    build_and_import('services/api/Dockerfile'),
    deps=['services/api', 'packages/core', 'pyproject.toml', 'uv.lock'],
    ignore=BUILD_IGNORE,
    skips_local_docker=True,
    disable_push=True,
    # Hai package đều được uv cài dạng editable (.pth trong site-packages trỏ
    # thẳng vào các thư mục này), nên chép file vào đây là đủ để process thấy —
    # không cần cài lại. uvicorn chạy với --reload (api.devReload trong
    # values-local.yaml) và watchfiles bắt sự kiện inotify do lần chép sinh ra.
    live_update=[
        sync('services/api/src', '/app/services/api/src'),
        sync('packages/core/src', '/app/packages/core/src'),
    ],
)

custom_build(
    'loom/web',
    build_and_import('web/Dockerfile'),
    deps=['web/src', 'web/index.html', 'web/package.json', 'web/package-lock.json',
          'web/vite.config.ts', 'web/tsconfig.json', 'web/tsconfig.app.json',
          'web/tsconfig.node.json', 'web/nginx.conf', 'web/public'],
    ignore=BUILD_IGNORE,
    skips_local_docker=True,
    disable_push=True,
    # Không có live_update: image runtime là nginx phục vụ bundle Vite đã build
    # sẵn. Sync mã nguồn TypeScript vào đó không đi qua bước build nào cả.
)

k8s_yaml(helm(
    'deploy/helm/loom',
    name='loom',
    namespace='loom',
    values=['deploy/envs/values-local.yaml'],
))

k8s_resource('loom-api', port_forwards=['8000:8000'], labels=['app'])
k8s_resource('loom-web', port_forwards=['8080:8080'], labels=['app'])

# Ở local chart không sinh Job migration (values-local đặt migration.enabled=false),
# vì Tilt áp dụng lại manifest liên tục còn Job thì bất biến. Đường Job vẫn được
# kiểm chứng trong CI bằng `helm template` + kubeconform, và Giai đoạn 6 sẽ chạy
# nó thật trong bài test e2e.
#
# `--context` tường minh: local_resource chạy lệnh shell trên host, hoàn toàn
# ngoài tầm với của allow_k8s_contexts ở trên. Không có cờ này thì một lần
# `kubectl config use-context` ở terminal khác đủ để lệnh migration bắn vào cụm
# thật — đúng cái tai nạn mà check-context sinh ra để chặn.
# TRIGGER_MODE_MANUAL có chủ đích. Database ở local KHÔNG phải container dùng
# xong vứt — nó là Aiven managed thật, cùng loại dịch vụ mà dev và prod dùng.
# Để mode tự động thì mỗi `make dev`, và mỗi lần sửa file trong loom-api, đều
# chạy DDL lên một database thật mà không ai bấm nút. Bấm một lần trong giao
# diện Tilt là đủ, và lần chạy đầu tiên trở thành một hành động có ý thức.
# Muốn tự động lại: xoá dòng trigger_mode.
local_resource(
    'migrate',
    cmd=('kubectl --context %s -n loom exec deploy/loom-api -- ' % EXPECTED_CONTEXT) +
        'sh -c "cd /app/services/api && alembic upgrade head"',
    resource_deps=['loom-api'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    labels=['app'],
)

print('Mở http://loom.localhost — đăng nhập long@loom.local / password')
print('Lần đầu: bấm chạy resource "migrate" trong Tilt để tạo bảng trên Aiven.')
