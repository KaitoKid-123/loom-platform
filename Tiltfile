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

# Ghi lại chủ đích, KHÔNG phải một lớp chặn thứ hai — đã kiểm và nó không chặn
# được gì mà `fail()` ở trên chưa chặn. allow_k8s_contexts chỉ THÊM vào danh
# sách cho phép, không thể từ chối: mọi context k3d/minikube/docker-desktop đều
# đã được Tilt coi là local và cho qua sẵn. Một cụm k3d tên khác, không nằm
# trong danh sách này, vẫn được chấp nhận. Thứ thật sự chặn là `fail()`.
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


def build_and_import(dockerfile, ref='$EXPECTED_REF'):
    """Build image rồi nạp vào node k3d, và kiểm chứng là nó thật sự tới nơi.

    Starlark KHÔNG nối hai string literal đặt cạnh nhau như Python — 'a' 'b' là
    lỗi cú pháp ("got string literal, want ','"). Phải nối bằng '+' tường minh.

    `ref` mặc định là `$EXPECTED_REF` — biến mà Tilt đặt cho lệnh của
    `custom_build`. Một `local_resource` KHÔNG được Tilt đặt biến đó (nó không
    theo dõi image nào cả), nên chỗ gọi từ local_resource phải truyền tag tường
    minh; xem `loom-task-image` phía dưới cho lý do ảnh nạp đi đường đó.

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
        'docker build -f %s -t %s .' % (dockerfile, ref),
        'flock /tmp/loom-k3d-image-import.lock' +
        ' k3d image import %s -c %s -m direct' % (ref, K3D_CLUSTER),
        'docker exec %s crictl inspecti -q docker.io/%s >/dev/null || {' % (K3D_NODE, ref),
        '  echo "k3d báo import xong nhưng %s KHÔNG có trong %s' % (ref, K3D_NODE) +
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
    # packages/storagekit và packages/icebergkit thêm vào vì vòng đời warehouse:
    # loom-api giờ gọi thẳng `loom_iceberg.warehouse.create_warehouse`/
    # `ensure_bootstrapped` (xem `loom_api.warehouse_provisioning`) — thiếu hai
    # dòng này thì sửa `packages/icebergkit` mà Tilt không build lại `loom/api`,
    # cùng cạm bẫy mà `loom-query` từng gặp với `packages/core`.
    deps=['services/api', 'packages/core', 'packages/storagekit', 'packages/icebergkit',
          'pyproject.toml', 'uv.lock'],
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

custom_build(
    'loom/query',
    build_and_import('services/loom-query/Dockerfile'),
    # Bốn package workspace mà Dockerfile COPY vào layer dependency
    # (packages/core trực tiếp; sqlkit/storagekit/icebergkit — icebergkit lại
    # kéo storagekit — xem comment COPY trong chính Dockerfile), CỘNG
    # services/loom-query. Thiếu một trong bốn thì sửa packages/icebergkit mà
    # Tilt không build lại — cùng cạm bẫy `loom/api` từng gặp với packages/core.
    deps=['services/loom-query', 'packages/core', 'packages/sqlkit',
          'packages/storagekit', 'packages/icebergkit', 'pyproject.toml', 'uv.lock'],
    ignore=BUILD_IGNORE,
    skips_local_docker=True,
    disable_push=True,
    # Không devReload/live_update: khác `loom/api`, `loom-query` chưa có nhu
    # cầu hot-reload ở Giai đoạn 2b (không có `query.devReload` trong values) —
    # sửa mã nguồn thì Tilt build lại ảnh đầy đủ, cùng khuôn `loom/web`.
)

k8s_yaml(helm(
    'deploy/helm/loom',
    name='loom',
    namespace='loom',
    values=['deploy/envs/values-local.yaml'],
))

k8s_resource('loom-api', port_forwards=['8000:8000'], labels=['app'])
k8s_resource('loom-web', port_forwards=['8080:8080'], labels=['app'])
# Không port-forward: loom-query là ClusterIP nội bộ, không phải thứ trình
# duyệt/`curl` từ host gọi thẳng — xem docstring `query-service.yaml`.
k8s_resource('loom-query', labels=['app'])

# Ảnh nạp (`loom-task`, Giai đoạn 3a) đi bằng `local_resource`, KHÔNG bằng
# `custom_build`, và lý do là kiến trúc chứ không phải sở thích: `custom_build`
# gắn một image vào các resource k8s tham chiếu nó, còn ảnh này KHÔNG được chart
# triển khai — `loom-api` phóng một `Job` cho mỗi lần nạp và trỏ tới ảnh qua
# `Settings.task_image` (xem `loom_api.jobs.JobLauncher`). Một `custom_build`
# không có resource nào dùng tới là thứ Tilt phải tự xử lý bằng cách nào đó, và
# "bằng cách nào đó" không phải một hợp đồng để dựa vào.
#
# Việc nó vẫn phải chạy ở `tilt up`: `k3d image import` là cách DUY NHẤT ảnh tới
# được containerd của node (daemon docker của host không phải runtime của cụm —
# xem ghi chú của hai cờ skips_local_docker/disable_push ở trên), và nếu node
# không có ảnh thì pod nạp đầu tiên `ImagePullBackOff` đi hỏi docker.io. Nên
# KHÔNG có `auto_init=False` ở đây, khác `migrate` phía dưới.
#
# Tag viết tường minh vì local_resource không có `$EXPECTED_REF`, và nó phải khớp
# ảnh mà `loom-api` yêu cầu — lệch nhau thì Job hỏi một ảnh không có trong node.
# Từ Task 15 chuỗi đó KHÔNG còn đến từ mặc định của `loom_core.config.Settings.
# task_image` nữa: chart truyền `LOOM_TASK_IMAGE = task.image:task.tag` xuống env
# của `loom-api` (xem `api-deployment.yaml`), nên bản sao phải giữ khớp ở đây là
# `task.image`/`task.tag` trong `deploy/helm/loom/values.yaml` — hôm nay cả hai
# đường đều cho ra `loom/task:dev`.
local_resource(
    'loom-task-image',
    cmd=build_and_import('services/loom-task/Dockerfile', ref='loom/task:dev'),
    # Hai package workspace mà Dockerfile COPY vào layer dependency
    # (packages/core trực tiếp; packages/connectorkit — bản thân connectorkit lại
    # phụ thuộc loom-core), CỘNG services/loom-task. Cùng cạm bẫy `loom/api` từng
    # gặp với packages/core: thiếu một dòng ở đây thì sửa connector mà Tilt không
    # build lại ảnh nạp, và lần nạp kế tiếp chạy mã cũ.
    deps=['services/loom-task', 'packages/core', 'packages/connectorkit',
          'pyproject.toml', 'uv.lock'],
    ignore=BUILD_IGNORE,
    labels=['app'],
)

# Ở local chart không sinh Job migration (values-local đặt migration.enabled=false),
# vì Tilt áp dụng lại manifest liên tục còn Job thì bất biến. Đường Job vẫn được
# kiểm chứng trong CI bằng `helm template` + kubeconform, và Giai đoạn 6 sẽ chạy
# nó thật trong bài test e2e.
#
# `--context` tường minh: local_resource chạy lệnh shell trên host, hoàn toàn
# ngoài tầm với của allow_k8s_contexts ở trên. Không có cờ này thì một lần
# `kubectl config use-context` ở terminal khác đủ để lệnh migration bắn vào cụm
# thật — đúng cái tai nạn mà check-context sinh ra để chặn.
# Database ở local KHÔNG phải container dùng xong vứt — nó là Aiven managed
# thật, cùng loại dịch vụ mà dev và prod dùng. Nên migration chỉ chạy khi có
# người bấm nút, không bao giờ tự động.
#
# PHẢI CÓ CẢ HAI CỜ. `trigger_mode` một mình là một cái guard rỗng: nó chỉ điều
# khiển việc chạy LẠI khi file thay đổi, còn lần chạy ĐẦU do `auto_init` quyết
# định và mặc định là True. Đã kiểm bằng thực nghiệm — chỉ trigger_mode thì
# resource vẫn chạy ngay lúc `tilt up`, thêm auto_init=False thì mới không.
local_resource(
    'migrate',
    cmd=('kubectl --context %s -n loom exec deploy/loom-api -- ' % EXPECTED_CONTEXT) +
        'sh -c "cd /app/services/api && alembic upgrade head"',
    resource_deps=['loom-api'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    auto_init=False,
    labels=['app'],
)

print('Mở http://loom.localhost — đăng nhập long@loom.local / password')
print('Lần đầu: bấm chạy resource "migrate" trong Tilt để tạo bảng trên Aiven.')

# ĐÃ KIỂM, và không ai đoán được:
#
# 1. Dừng `tilt up` (Ctrl-C hoặc SIGINT) XOÁ THEO `loom-api` và `loom-web` — nó không
#    chỉ tắt giao diện Tilt. `dex` sống sót vì do `make infra` tạo, không phải Tilt.
#    Muốn giữ app chạy mà KHÔNG giữ Tilt:
#
#        helm upgrade --install loom deploy/helm/loom -n loom \
#          -f deploy/envs/values-local.yaml
#
#    (cần loom/api:dev và loom/web:dev đã `k3d image import` vào node; mất hot reload)
#
# 2. Tilt giữ BỐN port trên host: 8080 (web), 8000 (api), 10350 (giao diện Tilt) và
#    một port ephemeral. Nếu 8080 đang bị thứ khác chiếm thì `tilt up` hỏng ở bước
#    port-forward chứ KHÔNG phải ở bước build — thông báo lỗi không chỉ về phía port,
#    nên chỗ này đã tốn thời gian gỡ một lần.
