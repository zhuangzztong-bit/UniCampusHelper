"""Offline regression checks for OpenVoice V2 downloads and cached voice profiles."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
venv_site = ROOT / ".venv" / "Lib" / "site-packages"
if venv_site.is_dir():
    sys.path.insert(0, str(venv_site))

import numpy as np

from unipus_helper import voice_cloner


class CachedEmbedding:
    """Supply the NumPy bridge used by the audio cache without importing torch."""

    def __init__(self, data):
        self.data = data

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.data


class VoiceAssetTests(unittest.TestCase):
    def setUp(self):
        temp_dir = self.enterContext(tempfile.TemporaryDirectory(prefix="voice-asset-check-"))
        self.root = Path(temp_dir)
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
        self.converter = voice_cloner.OpenVoiceV2Converter(self.model_dir)
        self.assets = {
            "config.json": json.dumps({
                "data": {"filter_length": 1024, "sampling_rate": 22050},
                "model": {"gin_channels": 256},
            }).encode("utf-8"),
            "checkpoint.pth": b"synthetic checkpoint data",
        }
        self.urls = {
            name: (f"https://voice-mirror.invalid/{name}", f"https://voice-official.invalid/{name}")
            for name in self.assets
        }
        self.manifest = {
            name: (len(data), hashlib.sha256(data).hexdigest(), self.urls[name])
            for name, data in self.assets.items()
        }
        self.enterContext(patch.object(voice_cloner, "OPENVOICE_V2_ASSETS", self.manifest))
        self.download = self.enterContext(patch.object(voice_cloner.urllib.request, "urlopen"))
        self.download.side_effect = self._serve_download
        self.remote = dict(self.assets)

    def _serve_download(self, request, **_kwargs):
        name = request.full_url.rsplit("/", 1)[-1]
        return io.BytesIO(self.remote[name])

    def _write_local_assets(self):
        for name, data in self.assets.items():
            (self.model_dir / name).write_bytes(data)

    def _inject_torch(self, loader_result, expected_checkpoint=None):
        torch = ModuleType("torch")
        torch.cuda = SimpleNamespace(is_available=lambda: False)
        torch.set_num_threads = Mock()
        torch.from_numpy = Mock(side_effect=CachedEmbedding)

        def restricted_load(source, *, weights_only=None, **_kwargs):
            self.assertIs(weights_only, True, "torch.load must explicitly restrict deserialization")
            if expected_checkpoint is not None:
                self.assertEqual(source.read(), expected_checkpoint)
            return loader_result

        torch.load = Mock(side_effect=restricted_load)
        self.enterContext(patch.dict(sys.modules, {"torch": torch}))
        return torch

    def test_ready_only_for_identical_content(self):
        self._write_local_assets()
        self.assertTrue(self.converter.is_downloaded())
        for name, data in self.assets.items():
            with self.subTest(asset=name):
                (self.model_dir / name).write_bytes(b"x" * len(data))
                self.assertFalse(self.converter.is_downloaded())
                (self.model_dir / name).write_bytes(data)
                self.assertTrue(self.converter.is_downloaded())

    def test_rejects_same_length_corrupt_download(self):
        checkpoint = "checkpoint.pth"
        self.remote[checkpoint] = b"x" * len(self.assets[checkpoint])
        with self.assertRaises(RuntimeError):
            self.converter.ensure_downloaded()
        self.assertFalse((self.model_dir / checkpoint).exists())
        self.assertEqual(list(self.model_dir.glob("*.part")), [])
        self.assertFalse(self.converter.is_downloaded())

    def test_download_repairs_corrupt_local_checkpoint(self):
        self._write_local_assets()
        checkpoint = self.converter.ckpt_path
        checkpoint.write_bytes(b"x" * len(self.assets[checkpoint.name]))
        self.assertTrue(self.converter.ensure_downloaded())
        self.assertEqual(checkpoint.read_bytes(), self.assets[checkpoint.name])
        self.assertTrue(self.converter.is_downloaded())

    def test_invalid_mirror_falls_back_to_verified_source(self):
        checkpoint = "checkpoint.pth"
        requested = []

        def serve(request, **kwargs):
            if request.full_url.endswith(checkpoint):
                requested.append(request.full_url)
                if request.full_url == self.urls[checkpoint][0]:
                    return io.BytesIO(self.assets[checkpoint][:-1])
            return self._serve_download(request, **kwargs)

        self.download.side_effect = serve
        self.assertTrue(self.converter.ensure_downloaded())
        self.assertEqual(requested, list(self.urls[checkpoint]))
        self.assertEqual(self.converter.ckpt_path.read_bytes(), self.assets[checkpoint])

    def test_corrupt_local_checkpoint_never_reaches_torch(self):
        self._write_local_assets()
        torch = self._inject_torch({})
        with patch.object(self.converter, "_fingerprint", return_value=(1,)):
            self.assertTrue(self.converter.is_downloaded())
            self.converter.ckpt_path.write_bytes(b"x" * len(self.assets["checkpoint.pth"]))
            self.assertTrue(self.converter.is_downloaded())
            with self.assertRaises(RuntimeError):
                self.converter._load()
            self.assertFalse(self.converter.is_downloaded())
        torch.load.assert_not_called()

    def test_cancelled_download_stops_before_content_is_published(self):
        stop = threading.Event()

        def cancel_during_download(pct, _message):
            if pct == 0.04:
                stop.set()

        with self.assertRaises(InterruptedError):
            self.converter.ensure_downloaded(cancel_during_download, stop)
        self.download.assert_called_once()
        self.assertEqual(list(self.model_dir.iterdir()), [])

    def test_repeated_readiness_reuses_validation(self):
        self._write_local_assets()
        fstat = os.fstat

        def different_ctime(descriptor):
            info = fstat(descriptor)
            values = {field: getattr(info, field) for field in
                      ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}
            values["st_ctime_ns"] += 1000
            return SimpleNamespace(**values)

        with (patch.object(voice_cloner.hashlib, "file_digest", wraps=hashlib.file_digest) as digest,
              patch.object(voice_cloner.os, "fstat", side_effect=different_ctime)):
            self.assertTrue(self.converter.is_downloaded())
            self.assertTrue(self.converter.is_downloaded())
            self.assertEqual(digest.call_count, len(self.assets))

    def test_verified_checkpoint_uses_restricted_loader(self):
        self._write_local_assets()
        state_dict = {"weight": object()}
        torch = self._inject_torch({"model": state_dict}, self.assets["checkpoint.pth"])
        model = Mock()
        model.to.return_value = model
        model.load_state_dict.return_value = ([], [])
        with patch.object(voice_cloner, "_build_openvoice_v2_classes", return_value=Mock(return_value=model)):
            self.converter._load()
        torch.load.assert_called_once()
        model.load_state_dict.assert_called_once_with(state_dict, strict=False)

    def test_legacy_target_embedding_rebuilds_without_deserializing_pickle(self):
        manager = voice_cloner.VoiceProfileManager(self.root / "profiles")
        profile = manager.profiles_dir / "legacy"
        profile.mkdir()
        (profile / "profile.json").write_text('{"se_ready": true}', encoding="utf-8")
        original_reference = b"synthetic reference audio"
        wav = profile / "reference_clean.wav"
        wav.write_bytes(original_reference)
        legacy = profile / "target_se.pt"
        legacy.write_bytes(b"synthetic legacy embedding")
        self.assertFalse(manager.get_profile("legacy")["has_se"])
        samples = np.linspace(-1, 1, 256, dtype=np.float32).reshape(1, 256, 1)
        embedding = CachedEmbedding(samples)
        torch = self._inject_torch(None)
        with (
            patch.object(voice_cloner, "decode_audio_to_float32") as decode,
            patch.object(self.converter, "extract_speaker_embedding", return_value=embedding) as extract,
        ):
            self.assertIs(manager.load_or_extract_se("legacy", self.converter), embedding)
            result = manager.load_or_extract_se("legacy", self.converter)
            self.assertTrue(manager.get_profile("legacy")["has_se"])
            decode.assert_called_once_with(str(wav), sample_rate=22050)
            extract.assert_called_once_with(decode.return_value)

        torch.load.assert_not_called()
        np.testing.assert_array_equal(result.numpy(), samples)
        self.assertEqual(legacy.read_bytes(), b"synthetic legacy embedding")
        self.assertEqual(wav.read_bytes(), original_reference)

        cache = Path(manager.get_profile("legacy")["se_path"])
        for invalid in (b"short", np.full(256, np.nan, dtype="<f4").tobytes(), b"x" * 1025):
            with self.subTest(cache_size=len(invalid)):
                cache.write_bytes(invalid)
                self.assertFalse(manager.get_profile("legacy")["has_se"])

    def test_new_recording_writes_and_replacement_invalidates_safe_embedding(self):
        manager = voice_cloner.VoiceProfileManager(self.root / "profiles")
        reference = self.root / "reference.wav"
        voice_cloner.write_wav_pcm16(reference, np.full(22050, 0.1, dtype=np.float32), 22050)
        self._write_local_assets()
        embedding = CachedEmbedding(np.full((1, 256, 1), 0.1, dtype=np.float32))

        with (
            patch.object(voice_cloner, "find_ffmpeg", return_value=""),
            patch.object(self.converter, "extract_speaker_embedding", return_value=embedding),
        ):
            saved = manager.save_from_audio_file(reference, converter=self.converter)
            self.assertTrue(saved["has_se"])
            cache = Path(saved["se_path"])
            self.assertEqual(cache.read_bytes(), embedding.numpy().astype("<f4").tobytes())

            replaced = manager.save_from_audio_file(reference)
            self.assertTrue(replaced["has_wav"])
            self.assertFalse(replaced["has_se"])
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
