"""MCP workspace packing behavior."""

from __future__ import annotations

import base64
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from colab_pool.utils import pack_local_dir_b64


class WorkspacePackingTests(unittest.TestCase):
    def test_local_directory_honors_default_and_cpoolignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            (root / ".git").mkdir(parents=True)
            (root / "__pycache__").mkdir()
            (root / "node_modules").mkdir()
            (root / "checkpoints").mkdir()
            (root / "data" / "raw").mkdir(parents=True)
            (root / "src").mkdir()

            (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            (root / "__pycache__" / "a.pyc").write_bytes(b"123")
            (root / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
            (root / "checkpoints" / "epoch1.pt").write_bytes(b"weights")
            (root / "data" / "raw" / "big.npy").write_bytes(b"raw")
            (root / "data" / "meta.json").write_text("{}", encoding="utf-8")
            (root / "src" / "model.py").write_text("print('model')", encoding="utf-8")
            (root / "train.py").write_text("print('train')", encoding="utf-8")
            (root / ".cpoolignore").write_text(
                "# ignore heavy artifacts\ncheckpoints/\n*.npy\n", encoding="utf-8"
            )

            archive = base64.b64decode(pack_local_dir_b64(root))
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
                names = set(tf.getnames())

            self.assertIn("train.py", names)
            self.assertIn("src/model.py", names)
            self.assertIn("data/meta.json", names)
            self.assertNotIn(".git/HEAD", names)
            self.assertNotIn("__pycache__/a.pyc", names)
            self.assertNotIn("node_modules/pkg.js", names)
            self.assertNotIn("checkpoints/epoch1.pt", names)
            self.assertNotIn("data/raw/big.npy", names)


if __name__ == "__main__":
    unittest.main()
