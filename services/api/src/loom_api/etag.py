"""ETag và `If-Match`, dùng chung cho mọi tài nguyên có version.

Ở một chỗ vì `item` và `workspace` đều cần, và hai bản sao của quy tắc này sẽ trôi khỏi
nhau — mà trôi ở đây nghĩa là một tài nguyên nhận `W/"7"` còn tài nguyên kia từ chối nó,
và client phải nhớ endpoint nào chấp nhận dạng nào.
"""

import re

from fastapi import HTTPException, status

# Nhận cả `W/"7"` và `7`. Client HTTP và proxy viết lại ETag thường xuyên, và biến một
# chi tiết định dạng thành 412 là cách chắc chắn để người dùng tin rằng công việc của họ
# vừa bị mất.
_ETAG_RE = re.compile(r'^(?:W/)?"?(\d+)"?\Z')


def etag_for(version: int) -> str:
    return f'W/"{version}"'


def parse_if_match(raw: str | None) -> int:
    if not raw:
        # 428 chứ không 400: nó nói cho client biết CHÍNH XÁC phải thêm header nào, và
        # một client tử tế sẽ tự thử lại đúng cách.
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "missing If-Match header — load the resource to get its ETag, then send it back",
        )
    match = _ETAG_RE.match(raw.strip())
    if not match:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed If-Match header: {raw}")
    return int(match.group(1))
