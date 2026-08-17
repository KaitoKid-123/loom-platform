"""Phép canh trần bộ nhớ của `loom-query` trong `deploy/helm/loom/values.yaml`.

Giai đoạn 2a đặt limit 384Mi từ một phép đo CHỈ soi DuckDB (`make measure-spill`:
`memory_limit=256MB`/`threads=2` trong một cgroup 384Mi). Phép đo đó đúng về DuckDB
và bỏ sót phần còn lại của đường đọc: PyIceberg/PyArrow đứng TRƯỚC DuckDB và giữ bộ
đệm riêng, **NGOÀI** `memory_limit` của DuckDB. ĐO 8
(`docs/measurements/2026-08-17-loom-query-memory.md`) đo trên cgroup thật của pod và
thấy trần đó thấp hơn nhu cầu của MỘT câu tầm thường: `SELECT count(*)` TRẦN trên
bronze 500.000 dòng cần `anon` 518 MiB / tổng 533 MiB, và ở 384Mi nó KHÔNG chạy được
— `memory.peak` đụng đúng 384,0 MiB, `oom_group_kill` +1 sau ~2 giây.

**Vì sao phép canh này KHÔNG canh `limit - đỉnh_đo_được >= X`, dù phép canh MinIO
canh đúng kiểu đó.** ĐO 8 đo được một tính chất làm hỏng cách viết ấy: **đỉnh PHÌNH
RA cho vừa cái trần được cấp.** Cùng tải K = 2 trên cùng bảng: trần 768Mi cho đỉnh
731,6 MiB (95%), trần 896Mi cho đỉnh 869,8 MiB (97%). Bộ cấp phát (mimalloc) chỉ trả
trang lại cho hệ điều hành khi bị ép, nên `memory.peak` đo "nó được phép lấy bao
nhiêu", không đo "tải cần bao nhiêu". Một phép canh dựa vào `limit - đỉnh` vì vậy tự
đuổi theo cái đuôi của nó: nâng trần làm đỉnh tăng theo và khoảng dư không bao giờ
lớn lên.

Nên phép canh này ghim thứ ỔN ĐỊNH: **ba phần cấu thành nhu cầu THẬT, và quan hệ
giữa chúng với trần cứng.** Chỉ một trong ba phần có giới hạn mà bộ cấp phát tuân
theo (`_MEMORY_LIMIT` của DuckDB); hai phần kia nằm ngoài mọi giới hạn mềm, và đó
CHÍNH LÀ điều khiến trần cứng phải phủ cả ba.

**Không có `GOMEMLIMIT` tương đương ở đây.** MinIO là Go: `GOMEMLIMIT` là giới hạn
MỀM mà bộ thu gom rác thật sự tuân theo, nên phép canh ở đó mua được "GC biết có
trần". Python không có thứ tương đương — `pa.default_memory_pool()` không nhận trần,
và `runner._release_arrow_memory()` chỉ trả lại phần ĐÃ free CHỨ KHÔNG chặn phần đang
cấp. Nên phép canh này mua được ĐÚNG một điều: **trần cứng còn ở trên nhu cầu đã đo,
và không ai hạ nó (hay nâng ngân sách DuckDB) mà không đụng vào con số ở đây.** Nó
KHÔNG mua được "không thể OOM": ba câu quét đồng thời vẫn OOMKill ở 768Mi, đo được.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from loom_query.runner import _MEMORY_LIMIT

REPO_ROOT = Path(__file__).resolve().parents[3]
VALUES = REPO_ROOT / "deploy" / "helm" / "loom" / "values.yaml"

# --- Ba phần cấu thành nhu cầu của MỘT câu quét, cả ba đều ĐO ĐƯỢC -----------
#
# (1) Nền lúc pod vừa khởi động, CHƯA phục vụ câu nào: `anon` 92,9 MiB đọc từ
#     cgroup pod. Đây là interpreter + FastAPI + cây phụ thuộc (duckdb, pyarrow,
#     pyiceberg đã import). Không co lại được.
COLD_BASELINE_MIB = 93

# (2) Đỉnh của POOL PyArrow trong một lần quét bảng 500k dòng, đọc từ
#     `pa.default_memory_pool().max_memory()`: đo được 232,6 / 247,9 / 264,6 /
#     266,2 / 296,6 MiB qua năm câu liên tiếp. Lấy mốc CAO NHẤT.
#
#     Phần này nằm HOÀN TOÀN ngoài `memory_limit` của DuckDB, và đó là chỗ phép đo
#     Giai đoạn 2a bỏ sót. Nó lớn vì `Lakehouse.scan()` không đẩy phép chiếu xuống
#     Iceberg: `count(*)` vẫn giải nén cả bảy cột, và PyIceberg vật chất hoá TRỌN
#     một data file mỗi lần (`list(...)` trong `ArrowScan.to_record_batches`), nên
#     con số này tỉ lệ với kích thước file lớn nhất nhân số cột.
ARROW_SCAN_PEAK_MIB = 297

# (3) Ngân sách của DuckDB không phải một hằng số ở đây — nó ĐỌC TỪ `runner.py`.
#     Ghim lại nó bằng cách import: nâng `_MEMORY_LIMIT` mà không nâng trần
#     container phải làm phép canh này đỏ, vì hai con số đó là một cặp.
#
# Trần cứng nhỏ nhất mà tại đó "một bước SQL của pipeline + MỘT câu tương tác"
# (K = 2) đã được KIỂM là chạy được, 3/3 lần: 768Mi. Vì sao con số này phải có mặt
# như một sàn RIÊNG, không suy ra từ phép cộng ba phần trên: trần đồng thời
# (`Settings.pipeline_concurrency_cap`) chỉ chặn phần đóng góp CỦA SCHEDULER —
# `/api/v1/query` không đi qua `decide()` — nên K = 2 là hình dạng người dùng tạo
# ra được BẤT KỂ trần đó bằng bao nhiêu. Một trần chỉ phủ K = 1 là một trần bỏ ngỏ
# đúng lỗi mà ĐO 7 đã ghi là N = 1 KHÔNG đóng được.
VERIFIED_K2_LIMIT_MIB = 768

# Khoảng dư tối thiểu trên phép cộng ba phần. Vì sao phải có: ĐO 7 đo được ranh
# giới hỏng là NGẪU NHIÊN — cùng cấu hình, ba câu đồng thời sống ở lần 1 và chết ở
# lần 2 — nên một trần đặt SÁT nhu cầu đã cộng nằm đúng trong vùng ngẫu nhiên đó.
MIN_HEADROOM_MIB = 64


def _quantity_to_mib(value: str) -> int:
    """Đổi một lượng bộ nhớ sang MiB, hiểu cả cách viết của Kubernetes và của DuckDB.

    Đây chính là cái bẫy mà phép canh MinIO đã ghi lại, và nó có thật ở đây nữa:
    Kubernetes viết `768Mi` (nhị phân), DuckDB viết `256MB` (THẬP PHÂN — 256 triệu
    byte = 244 MiB, không phải 256 MiB). So sánh hai chuỗi đó, hay coi `MB` là
    `MiB`, thì sai 12 MiB ở đúng chỗ không được sai.
    """
    match = re.fullmatch(r"(\d+)\s*(Ki|Mi|Gi|KiB|MiB|GiB|K|M|G|KB|MB|GB)?B?", value.strip())
    if match is None:
        raise ValueError(f"không đọc được lượng bộ nhớ: {value!r}")
    number, unit = int(match.group(1)), (match.group(2) or "").removesuffix("B")
    factor = {
        "": 1 / 1024**2,
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "K": 1000 / 1024**2,
        "M": 1000**2 / 1024**2,
        "G": 1000**3 / 1024**2,
    }[unit]
    return int(number * factor)


def _duckdb_budget_mib() -> int:
    """Ngân sách DuckDB mỗi connection, đọc từ `runner._MEMORY_LIMIT`."""
    return _quantity_to_mib(_MEMORY_LIMIT)


def _single_query_demand_mib() -> int:
    """Nhu cầu của MỘT câu quét: nền lạnh + đỉnh pool Arrow + ngân sách DuckDB.

    Cộng CHỨ KHÔNG lấy max, và đó là điểm chính: ba phần này sống CÙNG LÚC trong
    một câu. `memory_limit` của DuckDB chi phối duy nhất phần của DuckDB — bộ đệm
    Arrow mà PyIceberg dựng để nạp reader vào DuckDB nằm ngoài nó, và nền
    interpreter thì nằm ngoài cả hai.
    """
    return COLD_BASELINE_MIB + ARROW_SCAN_PEAK_MIB + _duckdb_budget_mib()


@pytest.fixture(scope="module")
def query_resources() -> dict[str, Any]:
    values = yaml.safe_load(VALUES.read_text())
    resources = values["query"]["resources"]
    assert isinstance(resources, dict), "query.resources phải là một mapping"
    return resources


def test_hard_limit_clears_one_scanning_query_with_headroom(
    query_resources: dict[str, Any],
) -> None:
    """Trần cứng phải phủ nhu cầu ĐÃ ĐO của một câu quét, và trên nó một khoảng thật.

    Đây là phép canh bắt đúng lần hồi quy đã xảy ra: 384Mi được chọn từ một phép đo
    chỉ soi DuckDB (244 MiB ngân sách DuckDB + 140 MiB "cho phần còn lại"), rồi
    phần còn lại đo ra là 93 + 297 = 390 MiB — nhiều hơn cả cái trần.
    """
    hard = _quantity_to_mib(query_resources["limits"]["memory"])
    demand = _single_query_demand_mib()

    assert hard >= demand + MIN_HEADROOM_MIB, (
        f"limit {hard} MiB không phủ nổi nhu cầu đã đo của MỘT câu quét "
        f"({demand} MiB = nền lạnh {COLD_BASELINE_MIB} + đỉnh pool Arrow "
        f"{ARROW_SCAN_PEAK_MIB} + ngân sách DuckDB {_duckdb_budget_mib()}) cộng "
        f"{MIN_HEADROOM_MIB} MiB dư. Đo thật ở 384Mi: một `SELECT count(*)` trần "
        f"trên bronze 500.000 dòng đụng trần và bị OOMKill sau ~2 giây."
    )


def test_hard_limit_covers_a_pipeline_step_beside_an_interactive_query(
    query_resources: dict[str, Any],
) -> None:
    """Trần phải phủ K = 2, vì K = 2 KHÔNG cần trần đồng thời cho phép.

    Không thể suy ra từ phép cộng ở trên (hai câu không chạm đỉnh cùng lúc, nên
    2 x nhu cầu là quá dè dặt — đo được: K = 2 chạy được ở 768Mi, 3/3 lần). Nên
    con số ở đây là một SÀN ĐÃ KIỂM, không phải một phép nhân.

    Vì sao nó xứng đáng một phép canh riêng: ĐO 7 đã ghi rằng N = 1 KHÔNG đóng được
    lỗi này — `/api/v1/query` không đi qua `decide()`, nên một người bấm một câu
    truy vấn trong lúc bước SQL của pipeline đang chạy tạo ra K = 2 bất kể trần
    đồng thời bằng bao nhiêu. Hạ trần xuống dưới 768Mi là mở lại đúng lỗ đó.
    """
    hard = _quantity_to_mib(query_resources["limits"]["memory"])

    assert hard >= VERIFIED_K2_LIMIT_MIB, (
        f"limit {hard} MiB nằm dưới {VERIFIED_K2_LIMIT_MIB} MiB — mức thấp nhất mà "
        f"tại đó 'một bước SQL của pipeline + một câu tương tác' đã được kiểm là "
        f"chạy được (3/3). Trần đồng thời KHÔNG chặn được hình dạng này: "
        f"`/api/v1/query` không đi qua `decide()`."
    )


def test_duckdb_budget_leaves_room_for_the_memory_outside_it(
    query_resources: dict[str, Any],
) -> None:
    """`_MEMORY_LIMIT` của DuckDB phải chừa chỗ cho phần nằm NGOÀI nó.

    Phép canh này bắt kiểu hồi quy đối xứng với phép trên: không ai hạ trần
    container, nhưng có người NÂNG `_MEMORY_LIMIT` trong `runner.py` để một câu
    nặng khỏi spill — và vì con số đó là ngân sách DuckDB tự nguyện tuân theo, nâng
    nó lên quá phần còn lại của trần làm DuckDB tin rằng nó được phép lấy nhiều hơn
    chỗ thực sự có. Kết cục không phải spill mà là OOMKill: hạt nhân không đọc
    `memory_limit`.

    Đây là quan hệ GẦN NHẤT với "mềm dưới cứng" mà phép canh MinIO ghim được, và
    khác ở một chỗ phải nói rõ: `memory_limit` của DuckDB chỉ bao phần của DuckDB,
    nên nó phải nằm dưới `trần - (nền lạnh + đỉnh pool Arrow)`, không phải chỉ dưới
    trần.
    """
    hard = _quantity_to_mib(query_resources["limits"]["memory"])
    outside = COLD_BASELINE_MIB + ARROW_SCAN_PEAK_MIB
    budget = _duckdb_budget_mib()

    assert budget <= hard - outside - MIN_HEADROOM_MIB, (
        f"ngân sách DuckDB {budget} MiB (`_MEMORY_LIMIT={_MEMORY_LIMIT}`) không "
        f"còn chừa đủ chỗ: trần {hard} MiB trừ phần nằm NGOÀI nó ({outside} MiB = "
        f"nền lạnh {COLD_BASELINE_MIB} + đỉnh pool Arrow {ARROW_SCAN_PEAK_MIB}) "
        f"trừ {MIN_HEADROOM_MIB} MiB dư. DuckDB tuân thủ hoàn hảo con số của nó mà "
        f"tiến trình vẫn bị hạt nhân giết."
    )


def test_requests_do_not_exceed_limits(query_resources: dict[str, Any]) -> None:
    """Kubelet từ chối pod có request lớn hơn limit. Dễ gây ra khi sửa một trong
    hai con số mà quên con số kia — và lỗi chỉ lộ ra lúc apply, không phải ở CI."""
    assert _quantity_to_mib(query_resources["requests"]["memory"]) <= _quantity_to_mib(
        query_resources["limits"]["memory"]
    )


def test_decimal_and_binary_units_are_not_confused() -> None:
    """`256MB` (thập phân) và `256Mi` (nhị phân) KHÔNG được đọc thành cùng một số.

    Không phải phép canh cho đủ: `_MEMORY_LIMIT` viết bằng `MB` còn `values.yaml`
    viết bằng `Mi`, và cả ba phép canh trên so hai cách viết đó với nhau. Một bộ
    đọc coi `MB == MiB` làm ngân sách DuckDB đọc ra 256 thay vì 244 — sai 12 MiB
    theo hướng LẠC QUAN, đúng hướng không được phép sai.
    """
    assert _quantity_to_mib("256MB") == 244
    assert _quantity_to_mib("256Mi") == 256
    assert _quantity_to_mib("768Mi") == 768
    assert _quantity_to_mib("1Gi") == 1024
