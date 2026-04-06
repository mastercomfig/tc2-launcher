import ctypes
import os
from typing import Dict, Optional, Tuple

VK_STRUCTURE_TYPE_APPLICATION_INFO = 0
VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO = 1

VK_PHYSICAL_DEVICE_TYPE_OTHER = 0
VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU = 1
VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU = 2
VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU = 3
VK_PHYSICAL_DEVICE_TYPE_CPU = 4

AMD_VENDOR_ID = 0x1002
NVIDIA_VENDOR_ID = 0x10DE
INTEL_VENDOR_ID = 0x8086


def _make_version(major: int, minor: int, patch: int) -> int:
    return (major << 22) | (minor << 12) | patch


class VkApplicationInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("pApplicationName", ctypes.c_char_p),
        ("applicationVersion", ctypes.c_uint32),
        ("pEngineName", ctypes.c_char_p),
        ("engineVersion", ctypes.c_uint32),
        ("apiVersion", ctypes.c_uint32),
    ]


class VkInstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
        ("pApplicationInfo", ctypes.POINTER(VkApplicationInfo)),
        ("enabledLayerCount", ctypes.c_uint32),
        ("ppEnabledLayerNames", ctypes.POINTER(ctypes.c_char_p)),
        ("enabledExtensionCount", ctypes.c_uint32),
        ("ppEnabledExtensionNames", ctypes.POINTER(ctypes.c_char_p)),
    ]


class VkPhysicalDeviceProperties(ctypes.Structure):
    _fields_ = [
        ("apiVersion", ctypes.c_uint32),
        ("driverVersion", ctypes.c_uint32),
        ("vendorID", ctypes.c_uint32),
        ("deviceID", ctypes.c_uint32),
        ("deviceType", ctypes.c_uint32),
        ("deviceName", ctypes.c_char * 256),
        ("pipelineCacheUUID", ctypes.c_uint8 * 16),
        ("limits", ctypes.c_uint8 * 504),
        ("sparseProperties", ctypes.c_uint8 * 20),
    ]


def get_gpu_vendor_name(vendor_id: int) -> str:
    vendors = {
        AMD_VENDOR_ID: "AMD",
        NVIDIA_VENDOR_ID: "NVIDIA",
        INTEL_VENDOR_ID: "Intel",
    }
    return vendors.get(vendor_id, "Unknown")


def get_vulkan_info() -> Tuple[bool, Optional[Dict[str, str | int]]]:
    """
    Checks for Vulkan support and retrieves GPU vendor information.

    Returns:
        A tuple of (is_supported, gpu_info), where gpu_info is a dictionary
        containing 'name', 'vendor_id', and 'vendor_name' if a Vulkan-capable
        GPU is found, otherwise None.
    """
    try:
        if os.name == "nt":
            vk = ctypes.WinDLL("vulkan-1.dll")
        else:
            vk = ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return False, None

    app_info = VkApplicationInfo(
        sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,
        pNext=None,
        pApplicationName=b"Team Comtress 2",
        applicationVersion=1,
        pEngineName=b"Source",
        engineVersion=1,
        apiVersion=_make_version(1, 3, 0),
    )

    create_info = VkInstanceCreateInfo(
        sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        pNext=None,
        flags=0,
        pApplicationInfo=ctypes.pointer(app_info),
        enabledLayerCount=0,
        ppEnabledLayerNames=None,
        enabledExtensionCount=0,
        ppEnabledExtensionNames=None,
    )

    vk_instance = ctypes.c_void_p()

    # We must not define argtypes for vkCreateInstance statically if we check it right after,
    # because it might not even be exported. But if DLL loaded, it's there.
    if not hasattr(vk, "vkCreateInstance"):
        return False, None

    res = vk.vkCreateInstance(
        ctypes.pointer(create_info), None, ctypes.pointer(vk_instance)
    )
    if res != 0:
        return False, None

    if hasattr(vk, "vkEnumeratePhysicalDevices"):
        vk.vkEnumeratePhysicalDevices.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
    if hasattr(vk, "vkGetPhysicalDeviceProperties"):
        vk.vkGetPhysicalDeviceProperties.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(VkPhysicalDeviceProperties),
        ]
    if hasattr(vk, "vkDestroyInstance"):
        vk.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    try:
        gpu_count = ctypes.c_uint32(0)
        res = vk.vkEnumeratePhysicalDevices(
            vk_instance, ctypes.pointer(gpu_count), None
        )

        if res != 0 or gpu_count.value == 0:
            vk.vkDestroyInstance(vk_instance, None)
            return True, None

        physical_devices = (ctypes.c_void_p * gpu_count.value)()
        res = vk.vkEnumeratePhysicalDevices(
            vk_instance, ctypes.pointer(gpu_count), physical_devices
        )

        if res != 0:
            vk.vkDestroyInstance(vk_instance, None)
            return True, None

        type_score = {
            VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU: 4,
            VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU: 3,
            VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU: 2,
            VK_PHYSICAL_DEVICE_TYPE_CPU: 1,
            VK_PHYSICAL_DEVICE_TYPE_OTHER: 0,
        }
        vendor_score = {
            AMD_VENDOR_ID: 2,
            NVIDIA_VENDOR_ID: 3,
            INTEL_VENDOR_ID: 1,
        }
        best_score = -1
        best_device_info = None

        for i in range(gpu_count.value):
            props = VkPhysicalDeviceProperties()
            vk.vkGetPhysicalDeviceProperties(physical_devices[i], ctypes.pointer(props))

            score = type_score.get(props.deviceType, 0)
            if props.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU:
                score += vendor_score.get(props.vendorID, 0)
            if score > best_score:
                best_score = score
                vendor_id = props.vendorID
                best_device_info = {
                    "name": props.deviceName.decode("utf-8", errors="ignore"),
                    "vendor_id": vendor_id,
                    "vendor_name": get_gpu_vendor_name(vendor_id),
                }

        vk.vkDestroyInstance(vk_instance, None)

        return True, best_device_info
    except Exception:
        # In case of any weird errors calling Vulkan functions
        return True, None
