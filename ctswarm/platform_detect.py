"""Hardware and platform detection.

The only module in ctswarm that knows about physical machines. Everything else
consumes the immutable ``HostProfile`` this produces, so adding support for a new
accelerator means editing one file.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass, replace
from enum import Enum


class Accelerator(str, Enum):
    """How local inference is accelerated on this host."""

    CUDA = "cuda"
    METAL_MLX = "metal_mlx"
    ROCM = "rocm"
    CPU = "cpu"


@dataclass(frozen=True)
class HostProfile:
    """Immutable description of the machine ctswarm is running on."""

    os_name: str
    arch: str
    accelerator: Accelerator
    # Memory the accelerator can actually hold a model in, in GiB. For CUDA this
    # is dedicated VRAM. For Apple Silicon it is unified memory, which is shared
    # with the OS, so we report a usable fraction rather than the raw total.
    accel_memory_gb: float
    system_memory_gb: float
    gpu_name: str | None = None
    has_ollama: bool = False
    has_mlx: bool = False
    has_lmstudio: bool = False

    @property
    def is_apple_silicon(self) -> bool:
        return self.os_name == "Darwin" and self.arch in ("arm64", "aarch64")

    @property
    def local_backend(self) -> str:
        """Preferred local inference backend for this host.

        On Apple Silicon, MLX quants outperform GGUF-on-Metal meaningfully enough
        that we prefer MLX when it is installed, falling back to Ollama otherwise.
        """
        if self.is_apple_silicon and self.has_mlx:
            return "mlx"
        if self.is_apple_silicon and self.has_lmstudio and not self.has_ollama:
            return "lmstudio"
        if self.has_ollama:
            return "ollama"
        if self.has_mlx:
            return "mlx"
        return "none"

    def to_dict(self) -> dict:
        return {
            "os": self.os_name,
            "arch": self.arch,
            "accelerator": self.accelerator.value,
            "accel_memory_gb": round(self.accel_memory_gb, 1),
            "system_memory_gb": round(self.system_memory_gb, 1),
            "gpu_name": self.gpu_name,
            "local_backend": self.local_backend,
            "has_ollama": self.has_ollama,
            "has_mlx": self.has_mlx,
            "has_lmstudio": self.has_lmstudio,
        }


def _run(cmd: list[str], timeout: int = 10) -> str | None:
    """Run a command, returning stripped stdout or None on any failure.

    Detection must never raise. A machine that lacks a tool is a normal case, not
    an error, and a hung vendor CLI must not wedge bootstrap.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _detect_cuda() -> tuple[str | None, float]:
    """Return (gpu_name, vram_gib) for the largest visible NVIDIA GPU."""
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not out:
        return None, 0.0
    best_name, best_mem = None, 0.0
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            # nvidia-smi reports MiB.
            mem_gb = float(parts[1]) / 1024.0
        except ValueError:
            continue
        if mem_gb > best_mem:
            best_name, best_mem = parts[0], mem_gb
    return best_name, best_mem


def _detect_apple() -> tuple[str | None, float]:
    """Return (chip_name, usable_unified_memory_gib) on Apple Silicon.

    Unified memory is shared with the OS and every other process, so handing the
    full total to the model catalog would produce recommendations that swap. We
    reserve headroom the way Metal's own working-set limit does, keeping roughly
    75% available for model weights.
    """
    total_bytes = _run(["sysctl", "-n", "hw.memsize"])
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if not total_bytes:
        return chip, 0.0
    try:
        total_gb = int(total_bytes) / (1024**3)
    except ValueError:
        return chip, 0.0
    return chip, total_gb * 0.75


def _detect_system_memory_gb() -> float:
    system = platform.system()
    if system == "Darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        if raw:
            try:
                return int(raw) / (1024**3)
            except ValueError:
                pass
        return 0.0
    # Linux: read MemTotal directly rather than shelling out to free(1), which
    # varies in output format across distributions.
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024**2)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _detect_mlx() -> bool:
    """True when the mlx-lm server is importable on this interpreter."""
    if platform.system() != "Darwin":
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("mlx_lm") is not None
    except (ImportError, ValueError):
        return False


def _detect_lmstudio() -> bool:
    if shutil.which("lms"):
        return True
    if platform.system() != "Darwin":
        return False
    import os

    return os.path.isdir(os.path.expanduser("~/.lmstudio"))


def detect_host() -> HostProfile:
    """Probe the current machine. Never raises."""
    os_name = platform.system()
    arch = platform.machine()
    system_memory_gb = _detect_system_memory_gb()

    accelerator = Accelerator.CPU
    gpu_name: str | None = None
    accel_memory_gb = 0.0

    if os_name == "Darwin" and arch in ("arm64", "aarch64"):
        accelerator = Accelerator.METAL_MLX
        gpu_name, accel_memory_gb = _detect_apple()
    else:
        gpu_name, vram = _detect_cuda()
        if vram > 0:
            accelerator = Accelerator.CUDA
            accel_memory_gb = vram
        elif shutil.which("rocm-smi"):
            accelerator = Accelerator.ROCM
            # ROCm reporting varies enough across versions that we do not trust a
            # parsed number here. Treat as unknown and let the catalog fall back
            # to system memory, which is the conservative choice.
            accel_memory_gb = 0.0

    profile = HostProfile(
        os_name=os_name,
        arch=arch,
        accelerator=accelerator,
        accel_memory_gb=accel_memory_gb,
        system_memory_gb=system_memory_gb,
        gpu_name=gpu_name,
        has_ollama=shutil.which("ollama") is not None,
        has_mlx=_detect_mlx(),
        has_lmstudio=_detect_lmstudio(),
    )

    # A CPU-only or unknown-accelerator host can still run models out of system
    # RAM, just slowly. Report that honestly rather than claiming zero capacity,
    # so the catalog can offer small models instead of nothing.
    if profile.accel_memory_gb == 0.0:
        profile = replace(profile, accel_memory_gb=system_memory_gb * 0.5)

    return profile


if __name__ == "__main__":
    print(json.dumps(detect_host().to_dict(), indent=2))
