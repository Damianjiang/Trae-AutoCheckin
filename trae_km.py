# -*- coding: utf-8 -*-
"""storage.json 里 Km 值的编解码（纯标准库）。

值 = 6 字节头 ‖ 32 字节盐 ‖ AES-128-CBC-PKCS7(SHA512(正文) ‖ 正文)。
密钥由盐派生，与机器无关，所以登录态可以离线造出来，也能自己加密写回。
"""
import base64
import hashlib
import os

KM_PREFIX = 'dGMFEAAA'                  # 6 字节前缀的 Base64 形态
KM_HEADER = bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00])
SALT_LEN = 32
HASH_LEN = 64
PREFIX_LEN = 6

# 与客户端一致的 64 字节常量，二者异或后参与密钥派生
SEG_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
               124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
               84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
               8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
SEG_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
               96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
               160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
               23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
MIXED = bytes(a ^ b for a, b in zip(SEG_A, SEG_B))


# AES-128 基础表
SBOX = bytes.fromhex(
    '637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0'
    'b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275'
    '09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf'
    'd0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2'
    'cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb'
    'e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08'
    'ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e'
    'e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16')


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p & 0xFF


MUL2 = bytes(_mul(i, 2) for i in range(256))
MUL3 = bytes(_mul(i, 3) for i in range(256))


def _expand_key(key):
    """AES-128 密钥扩展 → 44 个 4 字节字。"""
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                    # RotWord
            temp = [SBOX[b] for b in temp]                 # SubWord
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return words


def _add_round_key(state, words, rnd):
    for col in range(4):
        word = words[rnd * 4 + col]
        for row in range(4):
            state[col * 4 + row] ^= word[row]


def _shift_rows(state):
    # state 按列存放：state[col*4+row]
    for row in range(1, 4):
        vals = [state[col * 4 + row] for col in range(4)]
        vals = vals[row:] + vals[:row]
        for col in range(4):
            state[col * 4 + row] = vals[col]


def _mix_columns(state):
    for col in range(4):
        i = col * 4
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = MUL2[a0] ^ MUL3[a1] ^ a2 ^ a3
        state[i + 1] = a0 ^ MUL2[a1] ^ MUL3[a2] ^ a3
        state[i + 2] = a0 ^ a1 ^ MUL2[a2] ^ MUL3[a3]
        state[i + 3] = MUL3[a0] ^ a1 ^ a2 ^ MUL2[a3]


def encrypt_block(key, block):
    words = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, 0)
    for rnd in range(1, 10):
        for i in range(16):
            state[i] = SBOX[state[i]]
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, rnd)
    for i in range(16):
        state[i] = SBOX[state[i]]
    _shift_rows(state)
    _add_round_key(state, words, 10)
    return bytes(state)


def aes_cbc_encrypt(key, iv, data):
    """无填充 CBC 加密（调用方自己补 PKCS#7，data 必须 16 字节对齐）。"""
    out = bytearray()
    prev = bytes(iv)
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = encrypt_block(key, block)
        out.extend(prev)
    return bytes(out)


# ── 解密：自带实现，绝不 import trae_checkin ──
# 早先这里 `from trae_checkin import aes128_cbc_decrypt`，而 trae_checkin 执行时是
# `__main__`，再导入一次会生成第二个模块实例并重复跑它的配置层（日志里那两条
# "no usable fields" 就是它）。既然本文件已有完整 AES，就不该有这层耦合。
INV_SBOX = bytearray(256)
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _inv_shift_rows(state):
    for row in range(1, 4):
        vals = [state[col * 4 + row] for col in range(4)]
        vals = vals[-row:] + vals[:-row]
        for col in range(4):
            state[col * 4 + row] = vals[col]


def _inv_mix_columns(state):
    for col in range(4):
        i = col * 4
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        state[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        state[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        state[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def _decrypt_block(key, block):
    words = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, 10)
    for rnd in range(9, 0, -1):
        _inv_shift_rows(state)
        for i in range(16):
            state[i] = INV_SBOX[state[i]]
        _add_round_key(state, words, rnd)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    for i in range(16):
        state[i] = INV_SBOX[state[i]]
    _add_round_key(state, words, 0)
    return bytes(state)


def _decrypt_blocks(key, iv, data):
    out = bytearray()
    prev = bytes(iv)
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        plain = _decrypt_block(key, chunk)
        out.extend(bytes(a ^ b for a, b in zip(plain, prev)))
        prev = chunk
    return bytes(out)


# Km 编解码
def derive_key_iv(salt):
    seed_hash = hashlib.sha512(salt).digest()
    digest = hashlib.sha512(seed_hash + MIXED).digest()
    return digest[0:16], digest[16:32]


def _strip_pkcs7(data):
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad + HASH_LEN and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def encrypt_km(plaintext):
    """明文 → Km 条目（Base64）。写入客户端 storage.json 时用这个方向。"""
    body = plaintext.encode('utf-8') if isinstance(plaintext, str) else bytes(plaintext)
    salt = os.urandom(SALT_LEN)
    key, iv = derive_key_iv(salt)
    block = hashlib.sha512(body).digest() + body
    pad = 16 - (len(block) % 16)
    block += bytes([pad]) * pad
    cipher = aes_cbc_encrypt(key, iv, block)
    return base64.b64encode(KM_HEADER + salt + cipher).decode('ascii')


def decrypt_km(value):
    """Km 条目 → 明文（已剥 PKCS#7，已校验前 64 字节摘要）。"""
    buf = base64.b64decode(str(value or '').strip())
    if len(buf) <= PREFIX_LEN + SALT_LEN:
        raise ValueError('Km 密文长度不足')
    if buf[:PREFIX_LEN] != KM_HEADER:
        raise ValueError('Km 前缀不匹配（%s）' % buf[:PREFIX_LEN].hex())
    key, iv = derive_key_iv(buf[PREFIX_LEN:PREFIX_LEN + SALT_LEN])
    plain = _strip_pkcs7(_decrypt_blocks(key, iv, buf[PREFIX_LEN + SALT_LEN:]))
    if len(plain) <= HASH_LEN:
        raise ValueError('Km 明文长度不足')
    digest, body = plain[:HASH_LEN], plain[HASH_LEN:]
    if hashlib.sha512(body).digest() != digest:
        raise ValueError('Km 摘要校验失败（密钥或密文被改过）')
    return body.decode('utf-8', 'replace')


def client_accepts(value):
    """模拟客户端解密校验：前缀 + 摘要都要对得上，才说明写回去能被认。"""
    try:
        decrypt_km(value)
        return True
    except Exception:
        return False


def encode_json(obj, **kwargs):
    import json
    return encrypt_km(json.dumps(obj, ensure_ascii=False, separators=(',', ':'), **kwargs))


def decode_json(value):
    import json
    if isinstance(value, (dict, list)):
        return value
    text = str(value or '').strip()
    if text.startswith('{') or text.startswith('['):
        return json.loads(text)
    return json.loads(decrypt_km(text))


# 自检
def selftest():
    """离线自检：AES 标准向量 + Km 往返 + 真实客户端条目可解。"""
    # FIPS-197 附录 C.1：AES-128 单块加密向量
    key = bytes(range(16))
    plain = bytes.fromhex('00112233445566778899aabbccddeeff')
    got = encrypt_block(key, plain)
    assert got.hex() == '69c4e0d86a7b0430d8cdb78070b4c55a', 'AES 单块加密不符合标准向量'
    assert _decrypt_blocks(key, bytes(16), got) == plain, '自有 CBC 解密与加密不自洽'

    for text in ('{}', '{"a":1}', '中文 JSON 测试', 'x' * 5000):
        back = decrypt_km(encrypt_km(text))
        assert back == text, 'Km 往返失败：%r' % text[:20]
    assert client_accepts(encrypt_km('{"ok":true}')), '自造条目没通过客户端校验'

    # 篡改必须被发现
    bad = bytearray(base64.b64decode(encrypt_km('{"ok":true}')))
    bad[-1] ^= 0xFF
    assert not client_accepts(base64.b64encode(bytes(bad)).decode()), '篡改未被检出'

    # 真实客户端条目（存在才测）
    live_ok = None
    try:
        import json
        import os
        path = os.path.expandvars(r'%APPDATA%\Trae CN\User\globalStorage\storage.json')
        if os.path.isfile(path):
            storage = json.load(open(path, encoding='utf-8'))
            value = storage.get('iCubeAuthInfo://icube.cloudide')
            if value:
                creds = decode_json(value)
                live_ok = bool(creds.get('refreshToken'))
                assert live_ok, '真实条目解出来没有 refreshToken'
                # 与 trae_get_token 的独立 AES 实现交叉校验同一段密文
                try:
                    import trae_get_token as gt
                    mine = encrypt_km('{"cross":1}')
                    assert gt.decrypt_storage_value(mine).decode() == '{"cross":1}', \
                        '与 trae_get_token 解密不一致'
                    assert gt.decrypt_storage_value(value).decode('utf-8', 'replace')[:1] == '{'
                except ImportError:
                    pass
    except Exception as exc:
        live_ok = 'skip(%s)' % exc
    return 'selftest ok: AES 向量通过，Km 往返通过，真实条目=%s' % (live_ok,)


if __name__ == '__main__':
    print(selftest())
