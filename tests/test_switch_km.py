# -*- coding: utf-8 -*-
"""换号 / 网页登录 / 隔离实例的核心逻辑单元测试（不联网、不写客户端目录）。

跑法：
    python -m unittest tests.test_switch_km -v
"""
import base64
import importlib.util
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KM = load_module("trae_km_t", "trae_km.py")
SWITCH = load_module("trae_switch_t", "trae_switch.py")
WEB = load_module("trae_weblogin_t", "trae_weblogin.py")
AUTO = load_module("trae_autologin_t", "trae_autologin.py")
CHECKIN = load_module("trae_checkin_t", "trae_checkin.py")


class KmCodecTests(unittest.TestCase):
    def test_aes128_matches_fips197_vector(self):
        key = bytes(range(16))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(
            KM.encrypt_block(key, plain).hex(),
            "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_roundtrip_various_payloads(self):
        for text in ("{}", '{"a":1}', "中文 JSON 测试", "x" * 4096):
            self.assertEqual(KM.decrypt_km(KM.encrypt_km(text)), text)

    def test_prefix_and_client_acceptance(self):
        value = KM.encrypt_km('{"ok":true}')
        self.assertTrue(value.startswith("dGMFEAAA"))
        self.assertTrue(KM.client_accepts(value))

    def test_tampered_ciphertext_rejected(self):
        raw = bytearray(base64.b64decode(KM.encrypt_km('{"ok":true}')))
        raw[-1] ^= 0xFF
        self.assertFalse(KM.client_accepts(base64.b64encode(bytes(raw)).decode()))

    def test_encode_decode_json_roundtrip(self):
        obj = {"userId": "123", "account": {"username": "测试"}}
        self.assertEqual(KM.decode_json(KM.encode_json(obj)), obj)
        # 明文 JSON 也要能读（部分版本直接写明文）
        self.assertEqual(KM.decode_json(json.dumps(obj)), obj)


class SynthStorageTests(unittest.TestCase):
    def _account(self, uid="1000000000000001"):
        priv, pub, _scalar, _point = WEB.gen_keypair()
        return {"name": "测试号", "username": "tester", "uid": uid,
                "refreshToken": "rt-" + uid, "accessToken": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.x",
                "deviceId": "1234567890123456", "realDeviceId": "1234567890123456",
                "deviceKeyPem": priv, "devicePubPem": pub, "machineId": "m" * 32}

    def test_rewrites_identity_keys_only(self):
        acc = self._account()
        storage = {
            "telemetry.machineId": "m" * 32,
            SWITCH.KEY_AUTH: KM.encode_json({"userId": "old", "refreshToken": "old"}),
            SWITCH.KEY_SERVER: KM.encode_json({"entitlementInfo": {}}),
            "keep.me": "1",
        }
        new = SWITCH.synth_storage(storage, acc)

        self.assertEqual(new["keep.me"], "1")
        self.assertNotIn(SWITCH.KEY_SERVER, new)                 # 上一个号的权益快照必须抹掉
        auth = KM.decode_json(new[SWITCH.KEY_AUTH])
        self.assertEqual(auth["userId"], acc["uid"])
        self.assertEqual(auth["refreshToken"], acc["refreshToken"])
        self.assertEqual(auth["account"]["username"], "tester")
        self.assertEqual(auth["host"], SWITCH.HOST)

        tags = KM.decode_json(new[SWITCH.KEY_TAG])
        self.assertEqual(tags[acc["uid"]], "cn")

        dev = KM.decode_json(new[SWITCH.PREFIX_DC + acc["realDeviceId"]])
        self.assertEqual(dev["privateKeyPEM"].strip(), acc["deviceKeyPem"].strip())
        for key in (SWITCH.KEY_AUTH, SWITCH.KEY_TAG,
                    SWITCH.PREFIX_DC + acc["realDeviceId"]):
            self.assertTrue(KM.client_accepts(new[key]), key)

    def test_falls_back_to_client_keypair_when_account_lacks_it(self):
        acc = self._account()
        donor_storage = {SWITCH.PREFIX_DC + acc["realDeviceId"]:
                         KM.encode_json({"privateKeyPEM": acc["deviceKeyPem"],
                                         "publicKeyPEM": acc["devicePubPem"]})}
        bare = dict(acc)
        bare.pop("deviceKeyPem")
        bare.pop("devicePubPem")
        new = SWITCH.synth_storage(donor_storage, bare)
        dev = KM.decode_json(new[SWITCH.PREFIX_DC + acc["realDeviceId"]])
        self.assertIn("BEGIN PRIVATE KEY", dev["privateKeyPEM"])

    def test_missing_device_raises(self):
        acc = self._account()
        acc.pop("realDeviceId")
        acc.pop("deviceId")
        with self.assertRaises(ValueError):
            SWITCH.synth_storage({}, acc)

    def test_auth_record_uses_real_username_not_remark(self):
        acc = self._account()
        acc["name"] = "我的小号"
        acc["username"] = "real_name"
        auth = SWITCH.build_auth_record(acc)
        self.assertEqual(auth["account"]["username"], "real_name")
        # 隐私字段必须清空，避免串号
        for key in ("email", "organization", "nonPlainTextMobile"):
            self.assertEqual(auth["account"][key], "")


class CarrierEntryTests(unittest.TestCase):
    def test_detect_and_expand(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "User" / "globalStorage").mkdir(parents=True)
            (root / "User" / "globalStorage" / "storage.json").write_text("{}", "utf-8")
            (root / "Local Storage" / "leveldb").mkdir(parents=True)
            (root / "Local Storage" / "leveldb" / "000001.log").write_text("x", "utf-8")
            (root / "Cache").mkdir()
            (root / "Cache" / "junk.bin").write_text("x", "utf-8")

            entries = SWITCH.detect_entries(str(root))
            self.assertIn("User/globalStorage/storage.json", entries)
            # 「分区本地状态」不算登录态载体
            self.assertFalse([e for e in entries if "local storage/leveldb" in e.lower()])
            files = SWITCH.expand_entries(str(root), ["User/globalStorage/storage.json"])
            self.assertEqual(files, ["User/globalStorage/storage.json"])


class WebDeviceTests(unittest.TestCase):
    def test_keypair_export_and_signature(self):
        priv, pub, scalar, point = WEB.gen_keypair()
        self.assertEqual(WEB.CK.ec_private_scalar(priv), scalar)
        self.assertEqual(WEB.pem_public_point(pub), point)
        data = b"canonical device proof"
        sig = WEB.CK.ec_sign(priv, data)
        self.assertTrue(WEB.verify_signature(pub, data, sig))
        self.assertFalse(WEB.verify_signature(pub, data + b"!", sig))

    def test_device_ids_are_distinct_and_valid(self):
        ids = {WEB.gen_device()["device_id"] for _ in range(8)}
        self.assertEqual(len(ids), 8)
        for item in ids:
            self.assertEqual(len(item), 16)
            self.assertTrue(item.isdigit())

    def test_authorize_url_carries_identity(self):
        dev = WEB.gen_device()
        url = WEB.build_url(dev, 17388, "trace-1", "challenge-1")
        self.assertIn("https://www.trae.cn/authorization?", url)
        self.assertIn("client_id=ono9krqynydwx5", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("auth_from=trae", url)
        self.assertIn(dev["device_id"], url)
        self.assertIn("17388", url)

    def test_port_fallback_returns_usable_port(self):
        port = WEB.pick_port(17388)
        self.assertGreater(port, 0)


class AutoProfileTests(unittest.TestCase):
    def test_slug_is_ascii_and_stable(self):
        a = AUTO.slugify("账号A")
        b = AUTO.slugify("账号A")
        self.assertEqual(a, b)
        self.assertTrue(a.isascii())
        self.assertNotEqual(a, AUTO.slugify("账号B"))

    def test_profile_dir_uses_stored_name(self):
        slot = {"name": "测试号", "profile": "p-1234"}
        self.assertTrue(AUTO.profile_dir(slot).endswith("p-1234"))
        self.assertIn(AUTO.PROFILE_ROOT, AUTO.profile_dir(slot))
        self.assertTrue(AUTO.storage_file(AUTO.profile_dir(slot))
                        .endswith(os.path.join("User", "globalStorage", "storage.json")))

    def test_child_env_drops_electron_pollution(self):
        env = AUTO.child_env({"ELECTRON_RUN_AS_NODE": "1", "NODE_OPTIONS": "--x", "PATH": "/bin"})
        for key in AUTO.DROP_ENV:
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/bin")


class DeviceIdentityTests(unittest.TestCase):
    """20403 回归：同设备号、不同客户端，密钥对可能不同，必须逐个试身份。"""

    def _identities(self):
        return [
            {"deviceId": "dev-A", "privateKeyPem": "k-other", "publicKeyPem": "p-other",
             "uid": "999", "product": "Trae CN"},
            {"deviceId": "dev-A", "privateKeyPem": "k-right", "publicKeyPem": "p-right",
             "uid": "111", "product": "TRAE SOLO CN"},
        ]

    def test_uid_matched_identity_ranks_first(self):
        acc = {"uid": "111", "deviceId": "dev-A", "refreshToken": "rt"}
        with patch.object(CHECKIN, "local_device_identities",
                                        return_value=self._identities()):
            cands = CHECKIN.identity_candidates(acc)
        self.assertEqual(cands[0]["tag"], "TRAE SOLO CN/dev-A")
        self.assertEqual(cands[0]["publicKeyPem"], "p-right")
        self.assertEqual(len(cands), 2)

    def test_account_owned_keys_come_before_client_scan(self):
        acc = {"uid": "111", "deviceId": "dev-A", "refreshToken": "rt",
               "deviceKeyPem": "k-own", "devicePubPem": "p-own", "machineId": "m"}
        with patch.object(CHECKIN, "local_device_identities",
                                        return_value=self._identities()):
            cands = CHECKIN.identity_candidates(acc)
        self.assertEqual(cands[0]["tag"], "账号自带")
        self.assertEqual(cands[0]["publicKeyPem"], "p-own")

    def test_ensure_token_probes_until_one_signs(self):
        # uid 对不上任何客户端时，顺序退回列表顺序：第一套必然失败，第二套命中
        acc = {"deviceId": "dev-A", "refreshToken": "rt"}
        identities = [
            {"deviceId": "dev-A", "privateKeyPem": "k1", "publicKeyPem": "p-other",
             "uid": "999", "product": "Trae CN"},
            {"deviceId": "dev-A", "privateKeyPem": "k2", "publicKeyPem": "p-right",
             "uid": "888", "product": "TRAE SOLO CN"},
        ]
        tried = []

        def fake_refresh(token, keys):
            tried.append(keys["publicKeyPem"])
            if keys["publicKeyPem"] == "p-right":
                return "new-at", "new-rt", "ok"
            return None, None, "refresh http=401 code=20403 Token device not match."

        with patch.object(CHECKIN, "local_device_identities", return_value=identities), \
                patch.object(CHECKIN, "refresh", side_effect=fake_refresh), \
                patch.object(CHECKIN, "put_cache"), \
                patch.object(CHECKIN, "persist_tokens"):
            ok, msg = CHECKIN.ensure_token(acc, {})

        self.assertTrue(ok)
        self.assertEqual(tried, ["p-other", "p-right"])     # 先失败的，再命中的
        self.assertEqual(acc["accessToken"], "new-at")
        self.assertEqual(acc["refreshToken"], "new-rt")
        self.assertEqual(acc["deviceId"], "dev-A")
        self.assertEqual(acc["deviceKeyPem"], "k2")

    def test_ensure_token_stops_on_network_error(self):
        acc = {"uid": "111", "deviceId": "dev-A", "refreshToken": "rt"}
        tried = []

        def fake_refresh(token, keys):
            tried.append(keys["publicKeyPem"])
            return None, None, "refresh http=-1 请求异常: timed out"

        with patch.object(CHECKIN, "local_device_identities",
                                        return_value=self._identities()), \
                patch.object(CHECKIN, "refresh", side_effect=fake_refresh), \
                patch.object(CHECKIN, "put_cache"), \
                patch.object(CHECKIN, "persist_tokens"):
            ok, _msg = CHECKIN.ensure_token(acc, {})

        self.assertFalse(ok)
        self.assertEqual(len(tried), 1)                    # 网络错误换身份没意义
        self.assertNotIn("accessToken", acc)


class CreditParseTests(unittest.TestCase):
    """积分解析：usage_summary 优先，包列表兜底（面板「刷新」用这条链）。"""

    def test_usage_summary(self):
        payload = {"usage_summary": {"total_amount": 500, "consumed_amount": 120.5}}
        self.assertAlmostEqual(CHECKIN.parse_credits(payload), 379.5)

    def test_data_wrapper(self):
        payload = {"data": {"usage_summary": {"total_amount": 10, "consumed_amount": 4}}}
        self.assertAlmostEqual(CHECKIN.parse_credits(payload), 6)

    def test_pack_list_fallback(self):
        payload = {"user_entitlement_pack_list": [
            {"entitlement_base_info": {"quota": {"credits_limit": 100}},
             "usage": {"credits_amount": 30}},
            {"entitlement_base_info": {"quota": {"credits_limit": 50}},
             "usage": {"credits_amount": 50}},
        ]}
        self.assertAlmostEqual(CHECKIN.parse_credits(payload), 70)

    def test_missing_returns_none(self):
        self.assertIsNone(CHECKIN.parse_credits({}))
        self.assertIsNone(CHECKIN.parse_credits({"usage_summary": {"total_amount": 0}}))
        self.assertIsNone(CHECKIN.parse_credits(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
