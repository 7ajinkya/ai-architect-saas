"""Build the demo Lambda deployment package deterministically.

Produces ``dist/demo-handler.zip`` containing only ``handler.py`` (the
content of ``demo_handler.py`` renamed to the Lambda entry point) plus
its sha256 hex digest on stdout.

Determinism contract: fixed entry name, fixed permissions, fixed
timestamp (1980-01-01, the earliest ZIP-legal date), sorted entries.
The same source always yields byte-identical output on any machine,
so the recorded sha256 is a stable content address the renderer can
trust without reading the operator's filesystem.

Usage:
    poetry run python infra/lambda/build_demo_artifact.py
        [--out-dir infra/lambda/dist]

The operator runs this once, then supplies the printed path + sha256
as the human-decided ``code_artifact_path`` / ``code_artifact_sha256``
provisioning inputs. The platform never builds or fetches code itself.
"""

import argparse
import hashlib
import pathlib
import zipfile

FIXED_DATE = (1980, 1, 1, 0, 0, 0)
ENTRY_NAME = "handler.py"
ENTRY_MODE = 0o644


def build_artifact(source: pathlib.Path, out_dir: pathlib.Path) -> tuple:
    """Build the zip deterministically. Returns (zip path, sha256 hex)."""
    content = source.read_bytes()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "demo-handler.zip"
    info = zipfile.ZipInfo(ENTRY_NAME, date_time=FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (ENTRY_MODE & 0xFFFF) << 16
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED
    ) as bundle:
        bundle.writestr(info, content)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return target, digest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the deterministic demo Lambda package."
    )
    parser.add_argument(
        "--out-dir",
        default=str(
            pathlib.Path(__file__).resolve().parent / "dist"
        ),
    )
    args = parser.parse_args()
    source = pathlib.Path(__file__).resolve().parent / "demo_handler.py"
    target, digest = build_artifact(
        source, pathlib.Path(args.out_dir)
    )
    print(f"artifact: {target}")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
