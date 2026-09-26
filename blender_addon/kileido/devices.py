"""Which device Cycles renders on."""

import bpy

GPU_BACKENDS = ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI")  # best first


def use_gpu():
    """True when Cycles can render on a GPU. The user's choice in Preferences wins;
    with none made (Blender's default, and the user's preference was found reset to
    None on 2026-09-25), the best backend's GPUs are enabled, never the CPU. Blender
    saves this with its other preferences on quit (Auto-Save Preferences)."""
    try:
        cycles = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return False
    try:
        chosen = cycles.compute_device_type
        if chosen != "NONE":
            cycles.refresh_devices()  # the device list fills lazily
            if any(device.use and device.type != "CPU" for device in cycles.get_devices_for_type(chosen)):
                return True
        for backend in GPU_BACKENDS:
            try:
                cycles.compute_device_type = backend
            except TypeError:  # not built into this Blender / this OS
                continue
            cycles.refresh_devices()
            gpus = [device for device in cycles.get_devices_for_type(backend) if device.type != "CPU"]
            if gpus:
                for device in cycles.get_devices_for_type(backend):
                    device.use = device.type != "CPU"
                return True
        cycles.compute_device_type = chosen
    except (AttributeError, TypeError, RuntimeError):
        pass
    return False
