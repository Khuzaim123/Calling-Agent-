"""Parser tests for the Redmi 9C harness using canned adb output (no device needed)."""
import importlib.util
import wave

from conftest import ROOT

spec = importlib.util.spec_from_file_location("redmi9c_checks", ROOT / "tools/device/redmi9c_checks.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

PM_LIST = """All Permissions:

+ permission:android.permission.CAPTURE_AUDIO_OUTPUT
  package:android
  label:null
  description:null
  protectionLevel:signature|privileged
+ permission:android.permission.RECORD_AUDIO
  package:android
  label:record audio
  description:null
  protectionLevel:dangerous
+ permission:android.permission.MODIFY_PHONE_STATE
  package:android
  label:null
  description:null
  protectionLevel:signature|privileged
"""


class FakeAdb:
    def __init__(self, responses):
        self.responses = responses

    def sh(self, cmd, timeout=30):
        for key, val in self.responses.items():
            if cmd.startswith(key):
                return val
        return ""


def test_t1_detects_32bit_userspace():
    adb = FakeAdb({"getprop ro.product.cpu.abilist": "armeabi-v7a,armeabi",
                   "getprop ro.product.model": "M2006C3MG"})
    r = h.t1_abi(adb)
    assert r["has_arm64_v8a"] is False and r["model_matches"] is True
    assert "cannot load" in r["verdict"]


def test_t1_detects_64bit():
    r = h.t1_abi(FakeAdb({"getprop ro.product.cpu.abilist": "arm64-v8a,armeabi-v7a,armeabi"}))
    assert r["has_arm64_v8a"] is True


def test_t2_parses_protection_levels():
    r = h.t2_permissions(FakeAdb({"pm list permissions -f": PM_LIST,
                                  "cmd role get-role-holders": "com.android.contacts"}))
    assert r["protection_levels"]["android.permission.CAPTURE_AUDIO_OUTPUT"] == "signature|privileged"
    assert r["privileged_only"]["android.permission.MODIFY_PHONE_STATE"] is True
    assert r["privileged_only"]["android.permission.RECORD_AUDIO"] is False
    assert r["dialer_role_holder"] == "com.android.contacts"


def test_recording_client_identification(tmp_path):
    adb = FakeAdb({
        "dumpsys audio": "mMode=MODE_IN_CALL\nRecordActivityMonitor: session:41 -- source:MIC -- uid:10234",
        "dumpsys media.audio_policy": "AudioRecordClient uid: 10234 silenced: true",
        "pm list packages -U": "package:com.example.recorder uid:10234\npackage:com.miui.voiceassist uid:10111",
        "pm list packages -s": "package:com.miui.voiceassist",
    })
    r = h._recording_clients(adb, tmp_path, "x")
    assert r["third_party_recorders"] == ["com.example.recorder"]
    assert any("silenced: true" in line for line in r["silenced_lines"])
    assert (tmp_path / "x_dumpsys_audio_policy.txt").exists()


def test_t4_signal(tmp_path):
    p = tmp_path / "b.wav"
    h._speech_like_wav(p, groups=(1, 2))
    with wave.open(str(p)) as w:
        assert w.getframerate() == 16000
        secs = w.getnframes() / 16000
    assert abs(secs - (3 * 0.6 + 2 * 1.5)) < 0.05
