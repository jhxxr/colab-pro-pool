"""Supported hardware references and prices observed on a single running VM."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from .db import PoolDatabase


# Device memory is a manufacturer reference, never a guarantee of a Colab VM.
# Supported identifiers follow googlecolab/google-colab-cli's Accelerator enum.
RESOURCE_CATALOG = (
    ("T4", "GPU", "NVIDIA T4", "16 GB GDDR6", "Free / Pro", "轻量推理、调试和小模型训练", "https://www.nvidia.com/en-us/data-center/tesla-t4/"),
    ("L4", "GPU", "NVIDIA L4", "24 GB GDDR6", "Pro", "推理、视觉任务与中等规模训练", "https://www.nvidia.com/en-us/data-center/l4/"),
    ("A100", "GPU", "NVIDIA A100", "40 / 80 GB HBM", "Pro", "大模型训练与较大批次实验", "https://www.nvidia.com/en-us/data-center/a100/"),
    ("H100", "GPU", "NVIDIA H100", "80 GB HBM3（SXM 参考）", "Pro+", "高吞吐训练；本池仅调度 Pro+", "https://www.nvidia.com/en-us/data-center/h100/"),
    ("G4", "GPU", "G4", "以运行探测结果为准", "Pro+", "大显存任务；本池仅调度 Pro+", "https://github.com/googlecolab/google-colab-cli"),
    ("CPU", "CPU", "CPU 运行时", "无 GPU 显存", "Free / Pro", "数据处理、下载和纯 CPU 实验", "https://research.google.com/colaboratory/faq.html"),
    ("v5e1", "TPU", "Google TPU v5e", "16 GiB HBM / 芯片", "Pro", "适配 JAX / XLA 的训练任务", "https://cloud.google.com/tpu/docs/v5e"),
    ("v6e1", "TPU", "Google TPU v6e", "32 GiB HBM / 芯片", "Pro", "新一代 TPU 训练任务", "https://cloud.google.com/tpu/docs/v6e"),
)


def sanitize_runtime_info(raw: str) -> dict[str, Any]:
    """Accept only bounded hardware fields from the remote log marker."""
    if len(raw) > 16384:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    for key, maximum in (("cpu_count", 4096), ("memory_total_bytes", 2**55),
                         ("disk_total_bytes", 2**60)):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum:
            result[key] = value
    devices = []
    raw_devices = data.get("gpu_devices", [])
    if isinstance(raw_devices, list):
        for gpu in raw_devices[:16]:
            if not isinstance(gpu, dict) or not isinstance(gpu.get("name"), str):
                continue
            device: dict[str, Any] = {"name": gpu["name"].strip()[:120]}
            if not device["name"]:
                continue
            memory = gpu.get("memory_total_mib")
            if isinstance(memory, (int, float)) and not isinstance(memory, bool) and 0 < memory <= 2**30:
                device["memory_total_mib"] = memory
            devices.append(device)
    result["gpu_devices"] = devices
    if not any(key in result for key in ("cpu_count", "memory_total_bytes", "disk_total_bytes")) and not devices:
        return {}
    result["detected_at"] = time.time()
    return result


def actual_resource_id(job: dict[str, Any]) -> str | None:
    devices = job.get("runtime_info", {}).get("gpu_devices", [])
    if not devices:
        return "CPU" if job.get("gpu", "").upper() == "CPU" and not job.get("tpu") else None
    name = devices[0]["name"].upper()
    for model in ("H100", "A100", "L4", "T4"):
        if re.search(rf"\b{model}\b", name):
            return model
    if "RTX PRO 6000" in name and str(job.get("gpu", "")).upper() == "G4":
        return "G4"
    return None


def record_usage_observation(
    db: PoolDatabase, name: str, generation: str, *, queried_at: float | None = None
) -> None:
    """An account total is a VM price only with one confirmed active assignment."""
    account = db.get_account(name)
    if account is None or account["cu_generation"] != generation:
        return
    if (account["status"] != "busy" or not account["current_job_id"]
            or account["cu_error"] or account["cu_active_assignments"] != 1):
        return
    job = db.get_job(account["current_job_id"])
    if job is None or job["status"] != "running" or job["assigned_account"] != name:
        return
    runtime = job.get("runtime_info", {})
    detected_at, observed_at = runtime.get("detected_at"), account["cu_updated_at"]
    rate = account["cu_consumption_rate"]
    if (not detected_at or observed_at is None or observed_at < detected_at
            or (queried_at is not None and queried_at < detected_at)
            or rate is None or not math.isfinite(rate) or rate < 0):
        return
    resource_id = actual_resource_id(job)
    if resource_id is None:
        return
    devices = runtime.get("gpu_devices", [])
    model = devices[0]["name"] if devices else "CPU"
    db.save_resource_observation({
        "job_id": job["job_id"], "resource_id": resource_id,
        "high_mem": int(job["high_mem"]), "actual_model": model,
        "runtime_info_json": json.dumps(runtime),
        "consumption_rate_hourly": rate, "observed_at": observed_at,
    })


def build_resource_catalog(db: PoolDatabase) -> dict[str, Any]:
    profiles: dict[str, list[dict[str, Any]]] = {}
    signatures: set[tuple[Any, ...]] = set()
    for observation in db.list_resource_observations():
        resource_id = observation["resource_id"]
        runtime = observation["runtime_info"]
        signature = (resource_id, observation["high_mem"], runtime.get("cpu_count"),
                     runtime.get("memory_total_bytes"),
                     runtime.get("disk_total_bytes"),
                     json.dumps(runtime.get("gpu_devices", []), sort_keys=True))
        if signature in signatures or len(profiles.get(resource_id, [])) >= 4:
            continue
        signatures.add(signature)
        profiles.setdefault(resource_id, []).append(observation)
    return {
        "resources": [
            {"id": item[0], "kind": item[1], "name": item[2], "device_memory": item[3],
             "recommended_tier": item[4], "description": item[5], "source_url": item[6],
             "gpu": "CPU" if item[1] == "TPU" else item[0],
             "tpu": item[0] if item[1] == "TPU" else "",
             "profiles": profiles.get(item[0], [])}
            for item in RESOURCE_CATALOG
        ],
        "pricing_note": "价格来自本池单实例运行时的实测 CU/h，保留采集时间与配置；再次分配时可能变化。尚无记录的型号显示待实测。",
        "configuration_note": "显存 / HBM 为芯片参考值；CPU、系统内存、磁盘和实际 GPU 以运行后探测结果为准。推荐等级不保证即时可用。",
    }
