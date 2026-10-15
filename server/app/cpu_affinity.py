"""CPU placement for queue processes; CPU IDs are OS logical processors."""
import ctypes
import json
import os

ROLES = ("api", "download", "details", "checks", "ai", "translation", "other")


def _windows_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    return kernel


def available_cpus():
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    if os.name == "nt":
        kernel = _windows_kernel()
        process, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(system)):
            raise OSError("Cannot read process CPU affinity")
        return [index for index in range(ctypes.sizeof(process) * 8) if process.value & (1 << index)]
    return list(range(os.cpu_count() or 1))


def apply(cpus):
    """Apply before creating any threads; descendants inherit the mask."""
    if not cpus:
        return False
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, set(cpus))
        return True
    if os.name == "nt":
        kernel = _windows_kernel()
        if not kernel.SetProcessAffinityMask(kernel.GetCurrentProcess(), sum(1 << ident for ident in cpus)):
            raise OSError("Cannot set process CPU affinity")
        return True
    return False


def plan(cpus, setting="auto"):
    cpus = sorted(set(cpus))
    if not cpus:
        raise ValueError("没有可用的逻辑 CPU")
    if setting == "off":
        return {role: [] for role in ROLES}
    if setting != "auto":
        try:
            value = json.loads(setting)
            if not isinstance(value, dict) or set(value) != set(ROLES):
                raise ValueError()
            for ids in value.values():
                if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)) or not set(ids) <= set(cpus):
                    raise ValueError()
            return {key: sorted(value[key]) for key in ROLES}
        except (ValueError, TypeError):
            raise ValueError("LIBRARY_CPU_AFFINITY 应为 auto、off 或包含 api 和六个队列的有效 CPU 编号 JSON") from None
    # Reserve API CPUs first. Small NAS hosts necessarily share some CPUs.
    reserved = 2 if len(cpus) >= 14 else 1
    result = {"api": cpus[:reserved]}
    remaining = cpus[reserved:] or cpus
    if len(remaining) < 6:
        for index, role in enumerate(ROLES[1:]):
            result[role] = [remaining[index % len(remaining)]]
    else:
        width = len(remaining) // 6
        for index, role in enumerate(ROLES[1:]):
            result[role] = remaining[index * width:(index + 1) * width]
        result["ai"] += remaining[6 * width:]
    return result


def shared(plan):
    seen = set()
    for ids in plan.values():
        if seen.intersection(ids):
            return True
        seen.update(ids)
    return False
