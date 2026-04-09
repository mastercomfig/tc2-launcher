import ctypes
import os
import traceback
from typing import Dict, Optional, Tuple

from tc2_launcher import logger
from tc2_launcher.env import get_safe_env, restore_system_env
from tc2_launcher.utils import DEV_INSTANCE

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


def _get_vulkan_info_internal() -> Tuple[
    bool, Optional[Dict[str, str | int]], Optional[str]
]:
    """
    Checks for Vulkan support and retrieves GPU vendor information.

    Returns:
        A tuple of (is_supported, gpu_info, error_msg), where gpu_info is a dictionary
        containing 'name', 'vendor_id', and 'vendor_name' if a Vulkan-capable
        GPU is found, otherwise None.
    """
    with restore_system_env():
        try:
            try:
                if os.name == "nt":
                    vk = ctypes.WinDLL("vulkan-1.dll")
                else:
                    try:
                        vk = ctypes.CDLL("libvulkan.so.1")
                    except OSError:
                        vk = ctypes.CDLL("libvulkan.so")
            except OSError:
                logger.error("Vulkan not found")
                logger.error(traceback.format_exc())
                return (
                    False,
                    None,
                    "Vulkan library not found. Please install Vulkan graphics drivers.",
                )

            required_ver_tup = (1, 3, 0)
            required_ver_str = ".".join(map(str, required_ver_tup))
            required_ver = _make_version(*required_ver_tup)

            app_info = VkApplicationInfo(
                sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,
                pNext=None,
                pApplicationName=b"Team Comtress 2",
                applicationVersion=1,
                pEngineName=b"Source",
                engineVersion=1,
                apiVersion=required_ver,
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
                logger.error("Vulkan library invalid")
                return False, None, "Vulkan library invalid."

            res = vk.vkCreateInstance(
                ctypes.pointer(create_info), None, ctypes.pointer(vk_instance)
            )
            if res != 0:
                logger.error(f"Could not create Vulkan instance: {res}")
                error_msg = f"Could not create Vulkan instance: {res}."
                if res == -9:  # VK_ERROR_INCOMPATIBLE_DRIVER
                    app_info.apiVersion = _make_version(1, 0, 0)
                    vk_instance_1_0 = ctypes.c_void_p()
                    test_res = vk.vkCreateInstance(
                        ctypes.pointer(create_info),
                        None,
                        ctypes.pointer(vk_instance_1_0),
                    )
                    if test_res != 0:
                        logger.error(
                            f"Vulkan 1.0 fallback instance creation failed: {test_res}"
                        )
                        if test_res == -9:
                            error_msg = "No Vulkan drivers were found on your system."
                        else:
                            error_msg = f"Vulkan driver error (code {test_res})."
                    else:
                        if hasattr(vk, "vkDestroyInstance"):
                            vk.vkDestroyInstance.argtypes = [
                                ctypes.c_void_p,
                                ctypes.c_void_p,
                            ]
                        vk.vkDestroyInstance(vk_instance_1_0, None)
                        supported_version_str = "1.0.0"
                        if hasattr(vk, "vkEnumerateInstanceVersion"):
                            vk.vkEnumerateInstanceVersion.argtypes = [
                                ctypes.POINTER(ctypes.c_uint32)
                            ]
                            api_version = ctypes.c_uint32(0)
                            if (
                                vk.vkEnumerateInstanceVersion(
                                    ctypes.pointer(api_version)
                                )
                                == 0
                            ):
                                v = api_version.value
                                supported_version_str = (
                                    f"{v >> 22}.{(v >> 12) & 0x3FF}.{v & 0xFFF}"
                                )
                        error_msg = (
                            f"Your Vulkan driver only supports Vulkan {supported_version_str}, "
                            f"but Vulkan {required_ver_str} is required."
                        )
                return False, None, error_msg

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
                    logger.error(f"Could not get physical device count: {res}")
                    return True, None, None

                physical_devices = (ctypes.c_void_p * gpu_count.value)()
                res = vk.vkEnumeratePhysicalDevices(
                    vk_instance, ctypes.pointer(gpu_count), physical_devices
                )

                if res != 0:
                    vk.vkDestroyInstance(vk_instance, None)
                    logger.error(f"Could not enumerate physical devices: {res}")
                    return True, None, None

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
                    vk.vkGetPhysicalDeviceProperties(
                        physical_devices[i], ctypes.pointer(props)
                    )

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
                logger.info(f"Found Vulkan device: {best_device_info}")
                return True, best_device_info, None
            except Exception:
                # In case of any weird errors calling Vulkan functions
                logger.error("Could not get Vulkan device properties")
                logger.error(traceback.format_exc())
                return True, None, None
        except Exception:
            logger.error("Error during Vulkan info retrieval")
            logger.error(traceback.format_exc())
            return False, None, "Error retrieving Vulkan information."


def get_vulkan_info() -> Tuple[bool, Optional[Dict[str, str | int]], Optional[str]]:
    """
    Checks for Vulkan support and retrieves GPU vendor information.
    On Linux, this is performed in a separate process.
    """
    import sys

    # Only use multi-process on Linux when bundled
    if os.name == "posix" and not DEV_INSTANCE:
        import json
        import subprocess

        try:
            # sys.executable points to the bundle executable
            # We call it with --vulkan-info which we added to __main__.py
            cmd = [sys.executable, "--vulkan-info"]
            logger.info(f"Running multi-process Vulkan check: {' '.join(cmd)}")

            # Use a safe environment that uses system libraries
            safe_env = get_safe_env()

            # Run the command and capture output
            result = subprocess.run(
                cmd,
                env=safe_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout.strip())
                    return (
                        data.get("is_supported", False),
                        data.get("gpu_info"),
                        data.get("error_msg"),
                    )
                except json.JSONDecodeError:
                    logger.error(
                        f"Failed to parse Vulkan check output: {result.stdout}"
                    )
            else:
                logger.error(
                    f"Vulkan check subprocess failed (code {result.returncode}): {result.stderr}"
                )
        except Exception as e:
            logger.error(f"Failed to run multi-process Vulkan check: {e}")

        logger.warning(
            "Vulkan multi-process check failed, falling back to internal check"
        )

    return _get_vulkan_info_internal()


def get_dx_info() -> Tuple[bool, Optional[Dict[str, str | int]], Optional[str]]:
    """
    Checks for DirectX 9 Shader Model 3 support and retrieves GPU vendor information.

    Returns:
        A tuple of (is_supported, gpu_info, error_msg), where gpu_info is a dictionary
        containing 'name', 'vendor_id', and 'vendor_name' if a SM3-capable
        GPU is found, otherwise None.
    """
    try:
        if os.name != "nt":
            return False, None, "DirectX is not supported on this OS."

        d3d9 = ctypes.windll.d3d9
    except Exception:
        logger.error("d3d9.dll not found")
        return False, None, "DirectX 9 is not installed."

    class D3DADAPTER_IDENTIFIER9(ctypes.Structure):
        _fields_ = [
            ("Driver", ctypes.c_char * 512),
            ("Description", ctypes.c_char * 512),
            ("DeviceName", ctypes.c_char * 32),
            ("DriverVersion", ctypes.c_int64),
            ("VendorId", ctypes.c_uint32),
            ("DeviceId", ctypes.c_uint32),
            ("SubSysId", ctypes.c_uint32),
            ("Revision", ctypes.c_uint32),
            ("DeviceIdentifier", ctypes.c_byte * 16),
            ("WHQLLevel", ctypes.c_uint32),
        ]

    try:
        Direct3DCreate9 = d3d9.Direct3DCreate9
        Direct3DCreate9.argtypes = [ctypes.c_uint32]
        Direct3DCreate9.restype = ctypes.c_void_p

        D3D_SDK_VERSION = 32
        pD3D9 = Direct3DCreate9(D3D_SDK_VERSION)

        if not pD3D9:
            logger.error("Direct3DCreate9 failed")
            return False, None, "Direct3DCreate9 failed."

        vtable_ptr = ctypes.cast(pD3D9, ctypes.POINTER(ctypes.c_void_p)).contents.value
        vtable = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))

        GetAdapterCountType = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
        GetAdapterCount = GetAdapterCountType(vtable[4])

        GetAdapterIdentifierType = ctypes.WINFUNCTYPE(
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(D3DADAPTER_IDENTIFIER9),
        )
        GetAdapterIdentifier = GetAdapterIdentifierType(vtable[5])

        GetDeviceCapsType = ctypes.WINFUNCTYPE(
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        GetDeviceCaps = GetDeviceCapsType(vtable[14])

        ReleaseType = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
        Release = ReleaseType(vtable[2])

        count = GetAdapterCount(pD3D9)
        D3DDEVTYPE_HAL = 1

        vendor_score = {
            AMD_VENDOR_ID: 2,
            NVIDIA_VENDOR_ID: 3,
            INTEL_VENDOR_ID: 1,
        }
        best_score = -1
        best_device_info = None

        for i in range(count):
            ident = D3DADAPTER_IDENTIFIER9()
            if GetAdapterIdentifier(pD3D9, i, 0, ctypes.byref(ident)) != 0:
                continue

            caps = (ctypes.c_uint32 * 200)()
            if GetDeviceCaps(pD3D9, i, D3DDEVTYPE_HAL, ctypes.byref(caps)) == 0:
                vs_ver = caps[49]
                vs_major = (vs_ver >> 8) & 0xFF

                # Check for Shader Model 3 (VS >= 3.0)
                if vs_major >= 3:
                    score = vendor_score.get(ident.VendorId, 0)
                    if score > best_score:
                        best_score = score
                        vendor_id = ident.VendorId
                        best_device_info = {
                            "name": ident.Description.decode("utf-8", errors="ignore"),
                            "vendor_id": vendor_id,
                            "vendor_name": get_gpu_vendor_name(vendor_id),
                        }

        Release(pD3D9)

        if best_device_info:
            logger.info(f"Found DirectX 9 SM3 device: {best_device_info}")
            return True, best_device_info, None
        else:
            logger.error("No DirectX 9 SM3 capable device found.")
            return (
                False,
                None,
                "Your GPU does not support DirectX 9 with Shader Model 3.0.",
            )

    except Exception as e:
        logger.error("Could not get DirectX 9 device properties")
        logger.error(traceback.format_exc())
        return False, None, f"DirectX initialization error: {e}"
