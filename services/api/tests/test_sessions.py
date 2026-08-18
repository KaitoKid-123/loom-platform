import pytest
from itsdangerous import BadSignature

from loom_api.sessions import CookieSigner, generate_pkce_pair

SECRET = "unit-test-secret"


def test_pkce_verifier_and_challenge_differ() -> None:
    verifier, challenge = generate_pkce_pair()
    assert verifier != challenge
    assert len(verifier) >= 43
    assert "=" not in challenge  # base64url không đệm


def test_pkce_pair_is_random_each_time() -> None:
    assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


def test_challenge_is_stable_for_a_given_verifier() -> None:
    from loom_api.sessions import challenge_for

    verifier, challenge = generate_pkce_pair()
    assert challenge_for(verifier) == challenge


def test_signer_roundtrip() -> None:
    signer = CookieSigner(SECRET, salt="tx")
    token = signer.dumps({"state": "abc", "verifier": "xyz"})
    assert signer.loads(token, max_age=600) == {"state": "abc", "verifier": "xyz"}


def test_signer_rejects_tampered_value() -> None:
    # LƯU Ý (đã đo, không phải suy đoán): tamper cũ ở đây từng là `token[:-2] + "xx"`
    # và fail ngẫu nhiên ~1/1024 lần chạy — KHÔNG deterministic. Nguyên nhân:
    # chữ ký HMAC-SHA1 dài 20 byte mã hoá thành 27 ký tự base64url không đệm;
    # 27*6 = 162 bit nhưng chỉ 160 bit là thật, nên KÝ TỰ CUỐI CÙNG (index -1)
    # có 2 bit thấp "don't care" (bit đệm, không mang thông tin). Đồng thời
    # itsdangerous (signer.py::verify_signature) base64-decode chữ ký RỒI MỚI
    # so sánh — tức so sánh byte đã giải mã, không so sánh chuỗi ký tự. Vì vậy
    # nếu ký tự áp chót của chữ ký gốc vốn đã là 'x' (xác suất 1/64) và ký tự
    # cuối gốc giải mã ra cùng 4-bit-cao với 'x' (xác suất 1/16, ví dụ gốc là
    # 'w'), thì `token[:-2] + "xx"` giải mã ra ĐÚNG byte chữ ký cũ — verify vẫn
    # pass dù chuỗi đã đổi. 1/1024 = 1/64 x 1/16 là số đo thực nghiệm cho
    # HMAC-SHA1 (digest 20 byte) của itsdangerous hiện tại; thuật toán băm
    # khác (digest length khác) sẽ cho một tỷ lệ khác, không nhất thiết là
    # 1/1024.
    #
    # Sửa: tamper vào ký tự áp chót (index -2) thay vì 2 ký tự cuối. Sextet
    # này nằm trọn trong nhóm base64 đầy đủ ý nghĩa của phần dư (nó góp bit
    # vào CẢ HAI byte cuối của chữ ký, không có bit đệm nào), nên đổi nó sang
    # bất kỳ ký tự nào khác ký tự gốc là CHẮC CHẮN làm đổi byte chữ ký đã giải
    # mã — đã kiểm chứng bằng cách quét 25.000 timestamp khác nhau, 0 lần
    # verify chấp nhận giá trị đã tamper (xem scratchpad sweep_tamper.py).
    signer = CookieSigner(SECRET, salt="tx")
    token = signer.dumps({"state": "abc"})
    tampered_char = "A" if token[-2] != "A" else "B"
    tampered = token[:-2] + tampered_char + token[-1]
    with pytest.raises(BadSignature):
        signer.loads(tampered, max_age=600)


def test_signer_rejects_tampered_payload() -> None:
    # Bổ sung tamper ở PHẦN PAYLOAD (đầu token) — đây là một cơ chế hỏng khác
    # với tamper chữ ký ở trên: HMAC được tính trên chuỗi "payload.timestamp"
    # dạng byte thô, CHƯA giải mã base64, nên không có vấn đề "bit don't
    # care" của base64 — đổi 1 ký tự bất kỳ trong payload chắc chắn làm sai
    # lệch input của HMAC và bị verify_signature từ chối.
    signer = CookieSigner(SECRET, salt="tx")
    token = signer.dumps({"state": "abc"})
    tampered_char = "A" if token[0] != "A" else "B"
    tampered = tampered_char + token[1:]
    with pytest.raises(BadSignature):
        signer.loads(tampered, max_age=600)


def test_signer_rejects_other_secret() -> None:
    token = CookieSigner(SECRET, salt="tx").dumps({"state": "abc"})
    with pytest.raises(BadSignature):
        CookieSigner("different-secret", salt="tx").loads(token, max_age=600)


def test_signer_salts_are_isolated() -> None:
    token = CookieSigner(SECRET, salt="tx").dumps({"state": "abc"})
    with pytest.raises(BadSignature):
        CookieSigner(SECRET, salt="session").loads(token, max_age=600)
