# -*- coding: utf-8 -*-
r"""
VMware Destroyer v3.0 -- VMware Workstation / Player 彻底卸载与残留清理

相对 v2.1 的核心修复
====================
1) vmusb.sys 之类的驱动"被占用删不掉"的根因是内核映像仍在加载。
   v2.1 只停了 8 个用户态服务，从未处理 vmusb / vmx86 / vmnet* / hcmon / vmci / vsock
   这些驱动服务，所以文件永远锁死（访问被拒绝）。
   v3.0 按"用户态服务 -> 网络驱动 -> USB/监控驱动 -> 引导型驱动"的顺序执行
   sc stop + 状态轮询 + Start=4 + sc delete，然后才删文件；仍失败则合并写入
   PendingFileRenameOperations，由系统在重启时删除。

2) 服务名写错：v2.1 把显示名（VMware NAT Service / VMware WSX Service）当成服务名，
   而且完全漏掉驱动。v3.0 动态枚举 Services 键（名称 / ImagePath / DisplayName 命中
   VMware 特征），并带 Hyper-V / VBS / 沙盒驱动保护名单，避免误伤
   vmbus、vmbusr、vmswitch、vmsmp、vmsproxy、vmgid 等系统组件。

3) 卸载码写死 6 个 GUID（与本机实际的 {86B6E794-CF20-4939-A2B4-1CD53C370315} 完全
   不匹配，等于没卸载任何东西）。v3.0 动态枚举 Uninstall 键，按 DisplayName /
   Publisher 匹配，先跑 msiexec /x 正规卸载，再清残留。

4) 补齐 PnP 层：pnputil 删除 VMware OEM 驱动包（oem*.inf）与幽灵设备节点
   （ROOT\VMWARE\*、ROOT\VMWVMCIHOSTDEV\*、USB\VID_0E0F&PID_0001\*）。

5) 补齐其余残留：DriverStore\FileRepository、服务事件日志源、SharedDLLs、MuiCache、
   Shell 关联、Installer\Products|Features|UpgradeCodes、Control\Class 虚拟网卡、
   Control\Network、NetworkList、防火墙规则、计划任务、__vmware_user__ 账户与
   __vmware__ 组。

6) 安全红线：绝不进入虚拟机目录（.vmx/.vmdk/.vmsd/.vmem/.nvram 一律不碰）；
   检测到运行中的虚拟机默认中止；--check 只检测不改动；所有删除都校验绝对路径；
   注册表按"键值数据里含 VMware"判定，不做整片删除；外部命令全部走参数数组，不经 shell。

7) 重启接力：无法当场删除的文件登记到 PendingFileRenameOperations + RunOnce，
   重启后自动执行 --post-reboot 二次清理并自注销，最后输出残留核验报告。

用法
====
    python vmware_destroyer_v3.py                 图形界面（推荐）
    python vmware_destroyer_v3.py --check         只检测，列出全部残留，不做任何改动
    python vmware_destroyer_v3.py --silent        控制台静默执行
    python vmware_destroyer_v3.py --silent --deep 额外清防火墙规则 / 计划任务 / 事件日志源
    python vmware_destroyer_v3.py --post-reboot   重启后接力（由 RunOnce 自动调用）

依赖
====
    必需：Python 3.8+（标准库即可跑）
    推荐：pywin32（服务控制更可靠）、psutil（进程识别更准）
    缺失时会自动降级到 sc.exe / taskkill 文本解析，不会中断流程。
"""

import argparse
import csv
import ctypes
import ctypes.wintypes as wt
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import winreg

try:
    import psutil
except Exception:
    psutil = None

try:
    import win32service
except Exception:
    win32service = None

APP_NAME = "VMware Destroyer"
APP_VERSION = "3.1.1"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4

SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
SERVICE_START_DISABLED = 4
SERVICE_CONTROL_STOP = 0x00000001

ROOT_NAMES = {
    winreg.HKEY_LOCAL_MACHINE: "HKEY_LOCAL_MACHINE",
    winreg.HKEY_CLASSES_ROOT: "HKEY_CLASSES_ROOT",
    winreg.HKEY_CURRENT_USER: "HKEY_CURRENT_USER",
}

VMWARE_GUID_LIKE = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")

# ---------------------------------------------------------------- 名称清单
# 用户态服务：已知顺序（先关仲裁/授权，再关网络服务）
VMWARE_USER_SERVICES = [
    "VMUSBArbService",
    "VMwareWorkstationServer",
    "VMwareHostd",
    "VMwareWSXService",
    "VMAuthdService",
    "VGAuthService",
    "VmwareAutostartService",
    "VMnetDHCP",
    "VMware NAT Service",
    "VMware",
    "vmware-usbarbitrator",
]

# 内核驱动：必须按依赖顺序拆（桥接 -> 用户态接口 -> 适配器 -> USB -> 监控 -> 虚拟机内核）
VMWARE_DRIVERS_ORDERED = [
    "vmnetbridge",
    "vmnetuserif",
    "vmnetadapter",
    "vmnet",
    "vmnetx",
    "vmusb",
    "hcmon",
    "vmx86",
    "vmci",
    "vsock",
    "VMparport",
    "vmkbd",
    "vmmouse",
    "vmaudio",
    "vmsgfx",
]

# 绝对不能碰的系统组件（Hyper-V / VBS / 内核隔离 / 沙盒 / WSL2）
PROTECTED_NAMES = {
    "vmbus", "vmbusr", "vmbusproxy", "vmbushid", "vmswitch", "vmsmp",
    "vmsproxy", "vmsp", "vmsvsf", "vmsvsp", "vmgid", "vmcompute",
    "vmwp", "vmstorfl", "vms3cap", "vmguest", "hvservice", "hvbus",
    "hvcrash", "hvsocket", "hvsocketcontrol", "hvsifiletrust", "hvhost",
    "hvslconn", "hvsocketmux", "hvcrashdmp",
}

VMWARE_HINT = re.compile(
    r"^(?!hv|hyper|mshv)(?:vmware|vmnet|vnetlib|vnetinst|vnetprobe|vnetprobelib|vmusb|vmx86|hcmon"
    r"|vmci|vsock|vmparport|vmauthd|vgauth|vmkbd|vmmouse|vmaudio|vmware-usbarbitrator)",
    re.I,
)

DRIVER_FILES = [
    "vmnet.sys", "vmnetx.sys", "vmnetadapter.sys", "vmnetbridge.sys",
    "vmnetuserif.sys", "vmusb.sys", "vmusbcc.sys", "hcmon.sys", "vmx86.sys",
    "vmci.sys", "vsock.sys", "VMparport.sys", "vmkbd.sys", "vmmouse.sys",
    "vmaudio.sys", "vmsgfx.sys",
]

SYSTEM32_FILES = [
    "vmnat.exe", "vmnetbridge.exe", "vmnetdhcp.exe", "vmnetdhcp.leases",
    "vmxw2ksetup.dll", "vnetprobe.exe", "vnetprobelib.dll", "vnetinst.dll",
    "vnetlib.dll", "vnetlib.exe", "vnetlib64.dll", "vnetinst64.dll",
    "VMwareCpl.dll", "vmwarecpl.dll",
]

VMWARE_PROCESSES = [
    "vmware.exe", "vmplayer.exe", "vmware-vmx.exe", "vmware-vmx-debug.exe",
    "vmware-vmx-stats.exe", "vmware-kvm.exe", "vmware-tray.exe",
    "vmware-system-tray.exe", "vmware-authd.exe", "vmware-usbarbitrator.exe",
    "vmware-usbarbitrator64.exe", "vmware-hostd.exe", "vmnetdhcp.exe",
    "vmnat.exe", "vmware-autostart.exe", "vmware-unity-helper.exe",
    "vmware-vmui.exe", "vnetlib.exe", "vmwareapp.exe",
]

PFRO_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager"
PFRO_VALUE = "PendingFileRenameOperations"
RUNONCE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE = "!VMwareDestroyerPostReboot"


# ---------------------------------------------------------------- 基础工具
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate():
    """以管理员身份重启自身。返回 True 表示已拉起新进程。"""
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            argv = sys.argv[1:]
        else:
            exe = sys.executable
            argv = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
        params = subprocess.list2cmdline(argv)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(rc) > 32
    except Exception:
        return False


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("mbcs", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("mbcs", "replace")


def run(argv, timeout=60):
    """执行外部命令。绝不经过 shell，避免路径与参数注入。返回 (rc, stdout, stderr)。"""
    try:
        cp = subprocess.run(
            argv,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        return cp.returncode, _decode(cp.stdout), _decode(cp.stderr)
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "executable not found"
    except Exception as exc:
        return -1, "", str(exc)


def powershell(script, timeout=120):
    return run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        timeout=timeout,
    )


def safe_listdir(directory):
    try:
        if directory and os.path.isdir(directory):
            return os.listdir(directory)
    except OSError:
        pass
    return []


def unique_paths(items):
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        try:
            key = os.path.normcase(os.path.abspath(item))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.abspath(item))
    return out


def is_vm_disk_path(path):
    """虚拟机目录/磁盘文件保护：任何 .vmx / .vmdk 及其所在目录都不许进删除清单。"""
    low = str(path).lower()
    return any(low.endswith(ext) for ext in (
        ".vmx", ".vmdk", ".vmsd", ".vmsn", ".vmem", ".nvram", ".vmtm",
        ".vmxf", ".vswp",
    ))


VM_ARTIFACT_EXTENSIONS = (
    ".vmx", ".vmdk", ".vmsd", ".vmsn", ".vmem", ".nvram", ".vmtm",
    ".vmxf", ".vswp",
)


def _is_reparse_point(path):
    """Fail closed on symlinks/junctions; never traverse or remove their targets."""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return True


def directory_contains_vm_artifacts(directory):
    """Protect a whole directory if it contains VM files or cannot be fully scanned."""
    if not os.path.isdir(directory):
        return False
    scan_errors = []

    def onerror(exc):
        scan_errors.append(exc)

    try:
        for root, dirs, files in os.walk(
                directory, topdown=True, onerror=onerror, followlinks=False):
            dirs[:] = [name for name in dirs
                       if not _is_reparse_point(os.path.join(root, name))]
            if any(name.lower().endswith(VM_ARTIFACT_EXTENSIONS) for name in files):
                return True
    except OSError:
        return True
    return bool(scan_errors)


# ---------------------------------------------------------------- 日志
class Logger:
    def __init__(self, path=None):
        self.path = path
        self.lines = []
        self._lock = threading.Lock()

    def __call__(self, message):
        line = str(message)
        with self._lock:
            self.lines.append(line)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except Exception:
                    pass

    def dump(self):
        with self._lock:
            return list(self.lines)


# ---------------------------------------------------------------- 文件删除
def clear_attrs(path):
    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
    except Exception:
        pass


def try_delete_file(path):
    """删除单个文件，返回 (ok, reason)。"""
    if not path or not os.path.isabs(path):
        return False, "非绝对路径，已跳过"
    if is_vm_disk_path(path):
        return False, "虚拟机磁盘/配置文件，受保护不删"
    reason = ""
    for _ in range(3):
        if not os.path.exists(path):
            return True, ""
        clear_attrs(path)
        try:
            os.remove(path)
            return True, ""
        except FileNotFoundError:
            return True, ""
        except OSError as exc:
            reason = str(exc)
            time.sleep(0.4)
    return False, reason or "未知错误"


def try_delete_dir(path, pending_sink=None):
    """删除目录（自底向上）。删不掉的文件交给 pending_sink 登记重启删除。"""
    if not path or not os.path.isabs(path):
        return False, "非绝对路径，已跳过"
    if not os.path.exists(path):
        return True, ""
    if not os.path.isdir(path):
        return try_delete_file(path)
    if _is_reparse_point(path):
        return False, "目录为符号链接/联接点，受保护不删"
    if directory_contains_vm_artifacts(path):
        return False, "目录含虚拟机文件或无法完整扫描，整目录受保护不删"
    stuck = []
    walked = []
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not _is_reparse_point(os.path.join(root, name))]
        walked.append((root, list(dirs), list(files)))
    for root, dirs, files in reversed(walked):
        for name in files:
            full = os.path.join(root, name)
            ok, _reason = try_delete_file(full)
            if not ok:
                stuck.append(full)
        for name in dirs:
            child = os.path.join(root, name)
            try:
                os.rmdir(child)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass
    if os.path.exists(path):
        if stuck and pending_sink:
            for item in stuck:
                pending_sink(item)
            return False, "有 %d 个文件被占用，已登记重启后删除" % len(stuck)
        return False, "目录未能完全删除"
    return True, ""


# ---------------------------------------------------------------- 重启删除队列
def read_pfro():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PFRO_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            data, vtype = winreg.QueryValueEx(key, PFRO_VALUE)
            if vtype == winreg.REG_MULTI_SZ and isinstance(data, list):
                return [str(x) for x in data]
    except Exception:
        pass
    return []


def add_pfro(paths):
    """把文件追加到 PendingFileRenameOperations（与系统已有条目合并，绝不覆盖）。"""
    clean = []
    for path in paths or []:
        if not path or not os.path.isabs(path):
            continue
        if os.path.exists(path):
            clean.append(os.path.abspath(path))
    if not clean:
        return 0
    existing = read_pfro()
    have = {str(item).strip().lower() for item in existing}
    added = 0
    for path in clean:
        tag = ("\\??\\" + path).lower()
        if tag in have:
            continue
        existing.append("\\??\\" + path)
        existing.append("")
        have.add(tag)
        added += 1
    if not added:
        return 0
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, PFRO_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, PFRO_VALUE, 0, winreg.REG_MULTI_SZ, existing)
        return added
    except Exception:
        return 0


def schedule_post_reboot(log=None):
    """写 HKLM RunOnce，重启后自动执行 --post-reboot 二次清理。"""
    try:
        if getattr(sys, "frozen", False):
            cmd = '"%s" --post-reboot --silent' % os.path.abspath(sys.executable)
        else:
            python = sys.executable
            cand = os.path.join(os.path.dirname(python), "pythonw.exe")
            if os.path.isfile(cand):
                python = cand
            cmd = '"%s" "%s" --post-reboot --silent' % (
                os.path.abspath(python), os.path.abspath(sys.argv[0]))
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, RUNONCE_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, RUNONCE_VALUE, 0, winreg.REG_SZ, cmd)
        if log:
            log("  已注册重启接力任务（RunOnce: %s）" % RUNONCE_VALUE)
        return True
    except Exception as exc:
        if log:
            log("  注册重启接力任务失败：%s" % exc)
        return False


def clear_post_reboot(log=None):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RUNONCE_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.DeleteValue(key, RUNONCE_VALUE)
        if log:
            log("  已注销重启接力任务")
    except Exception:
        pass


# ---------------------------------------------------------------- 占用者诊断
class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wt.DWORD), ("ProcessStartTime", wt.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wt.WCHAR * 256),
        ("strServiceShortName", wt.WCHAR * 64),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wt.ULONG),
        ("TSSessionId", wt.DWORD),
        ("bRestartable", wt.BOOL),
    ]


def holders_of(path):
    """用 Restart Manager 查文件占用者。对用户态文件/DLL 有效；内核驱动映像通常查不到。"""
    result = []
    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except Exception:
        return result
    try:
        rm.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.LPWSTR]
        rm.RmStartSession.restype = wt.DWORD
        rm.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT, ctypes.POINTER(wt.LPCWSTR),
                                           wt.UINT, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
        rm.RmRegisterResources.restype = wt.DWORD
        rm.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT),
                                 ctypes.POINTER(_RM_PROCESS_INFO), ctypes.POINTER(wt.DWORD)]
        rm.RmGetList.restype = wt.DWORD
        rm.RmEndSession.argtypes = [wt.DWORD]
        rm.RmEndSession.restype = wt.DWORD
    except Exception:
        return result

    handle = wt.DWORD(0)
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(handle), 0, key) != 0:
        return result
    try:
        names = (wt.LPCWSTR * 1)(path)
        if rm.RmRegisterResources(handle, 1, names, 0, None, 0, None) != 0:
            return result
        needed = wt.UINT(0)
        count = wt.UINT(0)
        reasons = wt.DWORD(0)
        rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if needed.value == 0:
            return result
        infos = (_RM_PROCESS_INFO * needed.value)()
        count = wt.UINT(needed.value)
        if rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), infos,
                        ctypes.byref(reasons)) != 0:
            return result
        for index in range(count.value):
            info = infos[index]
            app = info.strAppName or ""
            svc = info.strServiceShortName or ""
            result.append((int(info.Process.dwProcessId), app, svc))
    except Exception:
        return result
    finally:
        try:
            rm.RmEndSession(handle)
        except Exception:
            pass
    return result


# ---------------------------------------------------------------- 注册表工具
def _wow(root):
    return 0 if root == winreg.HKEY_CLASSES_ROOT else winreg.KEY_WOW64_64KEY


def enum_subkeys(root, subkey):
    out = []
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | _wow(root)) as key:
            index = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        pass
    return out


def reg_exists(root, subkey):
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | _wow(root)):
            return True
    except OSError:
        return False


def reg_values(root, subkey):
    values = {}
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | _wow(root)) as key:
            index = 0
            while True:
                try:
                    name, data, vtype = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = (data, vtype)
                index += 1
    except OSError:
        pass
    return values


def reg_mentions(root, subkey, pattern, depth=2):
    """键名、值名或值数据里是否出现指定特征（浅层递归，用于精确判断再删）。"""
    if pattern.search(subkey):
        return True
    for name, (data, _vtype) in reg_values(root, subkey).items():
        if pattern.search(str(name)):
            return True
        if isinstance(data, str) and pattern.search(data):
            return True
        if isinstance(data, (list, tuple)):
            for item in data:
                if pattern.search(str(item)):
                    return True
    if depth > 0:
        for child in enum_subkeys(root, subkey):
            if reg_mentions(root, subkey + "\\" + child, pattern, depth - 1):
                return True
    return False


def delete_key_tree(root, subkey):
    """自底向上递归删除注册表键。返回 (ok, reason)。"""
    if not reg_exists(root, subkey):
        return True, "不存在"
    stack = [subkey]
    collected = []
    while stack:
        current = stack.pop()
        collected.append(current)
        for child in enum_subkeys(root, current):
            stack.append(current + "\\" + child)
    ok = True
    reason = ""
    for item in reversed(collected):
        try:
            winreg.DeleteKeyEx(root, item, access=_wow(root), reserved=0)
        except FileNotFoundError:
            continue
        except OSError as exc:
            ok = False
            reason = str(exc)
    return ok, reason


def delete_reg_value(root, subkey, value_name):
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE | _wow(root)) as key:
            winreg.DeleteValue(key, value_name)
        return True
    except Exception:
        return False


def is_target_vmware_product(display_name, publisher):
    """Limit installer cleanup to VMware Workstation/Player, not every VMware app."""
    display = str(display_name or "").lower()
    vendor = str(publisher or "").lower()
    return ("vmware" in display or "vmware" in vendor) and (
        "workstation" in display or "player" in display)


# ---------------------------------------------------------------- 服务控制
def _open_scm(access=None):
    if not win32service:
        return None
    if access is None:
        access = win32service.SC_MANAGER_CONNECT
    try:
        return win32service.OpenSCManager(None, None, access)
    except Exception:
        return None


def query_state(name):
    """查询服务/驱动状态。返回 SERVICE_* 常量，不存在返回 None。"""
    if win32service:
        scm = _open_scm()
        if scm:
            try:
                handle = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
                try:
                    return int(win32service.QueryServiceStatus(handle)[1])
                finally:
                    win32service.CloseServiceHandle(handle)
            except Exception:
                pass
            finally:
                try:
                    win32service.CloseServiceHandle(scm)
                except Exception:
                    pass
    rc, out, _err = run(["sc", "query", name], timeout=20)
    if rc != 0:
        return None
    match = re.search(r"STATE\s*:\s*(\d+)", out)
    if not match:
        return None
    return int(match.group(1))


def stop_service(name, wait_seconds=25):
    """非阻塞下发停止指令 + 带超时轮询，绝不卡死界面。"""
    state = query_state(name)
    if state is None:
        return "未安装"
    if state == SERVICE_STOPPED:
        return "本来就已停止"
    sent = False
    if win32service:
        scm = _open_scm()
        if scm:
            try:
                handle = win32service.OpenService(
                    scm, name, win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS)
                try:
                    win32service.ControlService(handle, SERVICE_CONTROL_STOP)
                    sent = True
                finally:
                    win32service.CloseServiceHandle(handle)
            except Exception:
                sent = False
            finally:
                try:
                    win32service.CloseServiceHandle(scm)
                except Exception:
                    pass
    if not sent:
        rc, _out, _err = run(["sc", "stop", name], timeout=25)
        sent = rc == 0
    if not sent:
        return "停止指令发送失败"
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        current = query_state(name)
        if current is None:
            return "已停止（服务项已消失）"
        if current == SERVICE_STOPPED:
            return "已停止"
        time.sleep(0.4)
    return "停止超时"


def disable_service(name):
    """把 Start 改成 4（disabled），防止重启后又被拉起来。"""
    key = "SYSTEM\\CurrentControlSet\\Services\\" + name
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as handle:
            winreg.SetValueEx(handle, "Start", 0, winreg.REG_DWORD, SERVICE_START_DISABLED)
        return True
    except Exception:
        pass
    rc, _out, _err = run(["sc", "config", name, "start=", "disabled"], timeout=25)
    return rc == 0


def delete_service(name):
    if win32service:
        scm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
        if scm:
            try:
                handle = win32service.OpenService(scm, name, win32service.DELETE)
                try:
                    win32service.DeleteService(handle)
                    return True
                finally:
                    win32service.CloseServiceHandle(handle)
            except Exception:
                pass
            finally:
                try:
                    win32service.CloseServiceHandle(scm)
                except Exception:
                    pass
    rc, _out, _err = run(["sc", "delete", name], timeout=25)
    return rc == 0


def read_service_info(name):
    key = "SYSTEM\\CurrentControlSet\\Services\\" + name
    info = {"name": name, "image": "", "display": "", "type": None, "start": None}
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            found = True
            for value_name, attr in (("ImagePath", "image"), ("DisplayName", "display")):
                try:
                    data, _vtype = winreg.QueryValueEx(handle, value_name)
                    info[attr] = str(data)
                except OSError:
                    pass
            for value_name, attr in (("Type", "type"), ("Start", "start")):
                try:
                    data, _vtype = winreg.QueryValueEx(handle, value_name)
                    info[attr] = int(data)
                except OSError:
                    pass
    except OSError:
        return None
    return info if found else None


def collect_vmware_services(log=None):
    """只返回已知 Workstation/Player 服务和已验证的 VMware 驱动映像。"""
    users = []
    drivers = []
    known_user_services = {item.lower() for item in VMWARE_USER_SERVICES}
    for name in enum_subkeys(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services"):
        low = name.lower()
        if low in PROTECTED_NAMES:
            continue
        info = read_service_info(name)
        if info is None:
            continue
        stype = info.get("type")
        is_driver = False
        if isinstance(stype, int) and (stype & 0x0F) in (1, 2):
            is_driver = True
        if is_driver:
            if driver_file_of_service(name):
                drivers.append(name)
        elif low in known_user_services:
            users.append(name)

    def order(items, preferred):
        ranking = {item.lower(): index for index, item in enumerate(preferred)}
        return sorted(set(items), key=lambda item: (ranking.get(item.lower(), 999), item.lower()))

    users = order(users, VMWARE_USER_SERVICES)
    drivers = order(drivers, VMWARE_DRIVERS_ORDERED)
    if log:
        log("  枚举结果：用户态服务 %d 个，内核驱动 %d 个" % (len(users), len(drivers)))
        if users:
            log("    服务: " + ", ".join(users))
        if drivers:
            log("    驱动: " + ", ".join(drivers))
    return users, drivers


def driver_file_of_service(name):
    r"""Return only a known VMware driver under Windows\System32\drivers."""
    info = read_service_info(name)
    if not info:
        return ""
    image = (info.get("image") or "").strip()
    match = re.match(r'^"?(.+?\.sys)(?:"|\s|$)', image, re.I)
    if not match:
        return ""
    image_path = os.path.expandvars(match.group(1).strip())
    system_root = os.path.abspath(os.environ.get("SystemRoot", r"C:\Windows"))
    if image_path.lower().startswith("\\systemroot\\"):
        image_path = os.path.join(system_root, image_path[len("\\SystemRoot\\"):])
    elif image_path.startswith("\\??\\"):
        image_path = image_path[4:]
    elif image_path.lower().startswith("system32\\"):
        image_path = os.path.join(system_root, image_path)
    if not os.path.isabs(image_path):
        return ""
    candidate = os.path.realpath(image_path)
    driver_root = os.path.realpath(os.path.join(system_root, "System32", "drivers"))
    if os.path.basename(candidate).lower() not in {item.lower() for item in DRIVER_FILES}:
        return ""
    try:
        if os.path.normcase(os.path.commonpath([driver_root, candidate])) != os.path.normcase(driver_root):
            return ""
    except (OSError, ValueError):
        return ""
    return candidate


# ---------------------------------------------------------------- 卸载引擎
class Engine:
    """纯逻辑层：只通过回调向外报告，不碰任何界面对象。"""

    def __init__(self, logger, status=None, progress=None, dry_run=False, deep=False,
                 force=False, restore_point=True, run_msi=True, post_reboot=False,
                 log_dir=None):
        self.log = logger
        self._status = status or (lambda text: None)
        self._progress = progress or (lambda value: None)
        self.dry_run = bool(dry_run)
        self.deep = bool(deep)
        self.force = bool(force)
        self.restore_point = bool(restore_point)
        self.run_msi = bool(run_msi)
        self.post_reboot = bool(post_reboot)
        self.log_dir = log_dir or os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"), "VMwareDestroyer", "logs")
        self.log_path = None
        self.backup_dir = None

        self.pending_files = []
        self.pending_services = []
        self.msi_cleanup_targets = []
        self._seen_keys = set()
        self.failed = []
        self.notes = []
        self.remaining = []
        self.verification_complete = False
        self.queue_registration_complete = False
        self.progress_value = 0.0
        self.counters = {"services": 0, "drivers": 0, "files": 0, "dirs": 0,
                         "regkeys": 0, "inf": 0, "devices": 0}

    # ---------------- 基础回调
    def status(self, text):
        self._status(text)

    def p(self, value, message=None):
        if message:
            self.log(message)
        self.progress_value = max(0.0, min(float(value), 100.0))
        self._progress(self.progress_value)

    # ---------------- 准备
    def prepare(self):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            self.log_dir = os.environ.get("TEMP", ".")
        self.log_path = os.path.join(
            self.log_dir, "destroy_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        if isinstance(self.log, Logger):
            self.log.path = self.log_path
        self.backup_dir = None if self.dry_run else os.path.join(
            self.log_dir, "backup_%s" % time.strftime("%Y%m%d_%H%M%S"))
        self.log("=" * 68)
        self.log("%s v%s 启动" % (APP_NAME, APP_VERSION))
        self.log("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self.log("模式: %s" % ("仅检测(dry-run)" if self.dry_run else "实际清理"))
        self.log("日志: %s" % self.log_path)
        self.log("=" * 68)
        return True

    def make_restore_point(self):
        if self.dry_run or not self.restore_point:
            return
        self.log("> 创建系统还原点（失败不影响后续清理）")
        script = ("try { Checkpoint-Computer -Description 'Before VMware Destroyer' "
                  "-RestorePointType MODIFY_SETTINGS -ErrorAction Stop; 'OK' } "
                  "catch { 'FAIL: ' + $_.Exception.Message }")
        rc, out, _err = powershell(script, timeout=600)
        text = (out or "").strip().splitlines()
        tail = text[-1] if text else ""
        if tail.startswith("OK"):
            self.log("  还原点已创建")
        else:
            self.log("  还原点未创建（系统保护可能未开启）：%s" % (tail or rc))

    def backup_registry(self):
        if self.dry_run:
            return
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
        except Exception:
            return
        users, drivers = collect_vmware_services()
        targets = [
            ("HKLM", r"SOFTWARE\VMware, Inc."),
            ("HKLM", r"SOFTWARE\WOW6432Node\VMware, Inc."),
            ("HKCU", r"Software\VMware, Inc."),
        ]
        for name in users + drivers:
            targets.append(("HKLM", r"SYSTEM\CurrentControlSet\Services" + "\\" + name))
        for hive, sub in targets:
            filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", sub)[:80] + ".reg"
            path = os.path.join(self.backup_dir, filename)
            run(["reg", "export", hive + "\\" + sub, path, "/y"], timeout=90)
        self.log("  注册表备份目录: %s" % self.backup_dir)

    # ---------------- 预检
    def find_running_vms(self):
        found = []
        if psutil:
            for proc in psutil.process_iter(["name", "pid"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if name in ("vmware-vmx.exe", "vmware-vmx-debug.exe",
                                "vmware-vmx-stats.exe", "vmware-kvm.exe"):
                        found.append("%s(pid=%s)" % (name, proc.info.get("pid")))
                except Exception:
                    continue
        else:
            rc, out, _err = run(["tasklist", "/fi", "IMAGENAME eq vmware-vmx.exe", "/nh"], timeout=30)
            if "vmware-vmx" in (out or "").lower():
                found.append("vmware-vmx.exe")
        return found

    def precheck(self):
        self.status("环境预检...")
        self.p(2, "> 阶段 1/9 环境预检")
        running = self.find_running_vms()
        if running:
            message = "检测到正在运行的虚拟机: " + ", ".join(running)
            self.log("! " + message)
            self.notes.append(message)
            if not self.force:
                self.log("! 停止 vmx86 / vmci 等驱动会导致宿主机蓝屏或虚拟机数据损坏。")
                self.log("! 请先关闭所有虚拟机（不是挂起，是彻底关机退出），或加 --force 强制继续。")
                self.status("已中止：请先关闭运行中的虚拟机")
                self.remaining.append(message)
                return False
            self.log("! 已指定 --force，继续执行。")
        if not is_admin():
            self.log("! 当前不是管理员权限，服务/注册表/系统目录操作会大量失败。")
            self.notes.append("非管理员权限运行")
        self.log("  预检通过")
        self.p(6)
        return True

    # ---------------- 阶段：msiexec 正规卸载
    def find_uninstall_entries(self):
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        found = []
        for root, base in roots:
            for name in enum_subkeys(root, base):
                sub = base + "\\" + name
                values = reg_values(root, sub)
                display = str(values.get("DisplayName", ("", 0))[0] or "")
                publisher = str(values.get("Publisher", ("", 0))[0] or "")
                if not is_target_vmware_product(display, publisher):
                    continue
                uninstall = str(values.get("UninstallString", ("", 0))[0] or "")
                windows_installer = values.get("WindowsInstaller", (0, 0))[0]
                found.append({"root": root, "sub": sub, "display": display,
                              "uninstall": uninstall, "windows_installer": windows_installer,
                              "entry_name": name})
        return found

    def stage_uninstall_msi(self):
        self.status("执行官方卸载程序...")
        self.p(18, "> 阶段 3/9 调用官方卸载（msiexec /x）")
        entries = self.find_uninstall_entries()
        if not entries:
            self.log("  未找到 VMware 卸载注册项，跳过官方卸载")
        for entry in entries:
            self.log("  发现产品: %s" % (entry["display"] or entry["sub"]))
            command = entry["uninstall"]
            msi_hint = (entry.get("windows_installer") == 1
                        or bool(re.search(r"(?i)\bmsiexec(?:\.exe)?\b", command or "")))
            code_match = VMWARE_GUID_LIKE.search(command or "")
            if not code_match and VMWARE_GUID_LIKE.fullmatch(entry.get("entry_name", "")):
                code_match = VMWARE_GUID_LIKE.fullmatch(entry["entry_name"])
            code = code_match.group(0) if code_match else ""
            if self.dry_run:
                if msi_hint and code:
                    self.log("  [dry-run] 将执行: msiexec /x %s /qn /norestart" % code)
                else:
                    self.log("  [dry-run] 非 MSI 或缺少有效产品码，跳过自动卸载")
                continue
            if not msi_hint or not code:
                self.log("  ! 无法确认 MSI 产品码，跳过自动卸载并保留卸载项")
                self.notes.append("未自动卸载（非 MSI/产品码不明确）: " +
                                  (entry["display"] or entry["sub"]))
                continue
            self.log("  执行: msiexec /x %s /qn /norestart" % code)
            rc, _out, _err = run(
                ["msiexec", "/x", code, "/qn", "/norestart"], timeout=1800)
            self.log("  msiexec 返回码: %s" % rc)
            if rc in (0, 3010, 1605, 1641):
                self.msi_cleanup_targets.append((entry["root"], entry["sub"]))
            else:
                self.failed.append(("msiexec " + code, "返回码 %s" % rc))
        self.p(22)

    # ---------------- 阶段：进程
    def discover_processes(self):
        """Return only exact VMware executable names, keyed by PID identity."""
        names = {item.lower() for item in VMWARE_PROCESSES}
        found = []
        if psutil:
            for proc in psutil.process_iter(["name", "exe", "pid", "create_time"]):
                try:
                    info = proc.info
                    name = (info.get("name") or "").lower()
                    pid = int(info.get("pid") or 0)
                    if name not in names or pid <= 0:
                        continue
                    found.append((pid, name, info.get("exe") or "", info.get("create_time")))
                except Exception:
                    continue
        else:
            _rc, out, _err = run(["tasklist", "/fo", "csv", "/nh"], timeout=60)
            for fields in csv.reader((out or "").splitlines()):
                if len(fields) < 2 or fields[0].strip().lower() not in names:
                    continue
                try:
                    pid = int(fields[1].strip().replace(",", ""))
                except ValueError:
                    continue
                found.append((pid, fields[0].strip().lower(), "", None))
        return sorted(found, key=lambda item: item[0])

    def kill_processes(self, processes):
        if not processes:
            self.log("  未发现正在运行的 VMware 进程")
            return []
        handled = []
        for pid, name, exe, created in processes:
            label = "%s(pid=%s)" % (name, pid)
            if self.dry_run:
                handled.append(label + "(dry-run)")
                continue
            ok = False
            if psutil:
                try:
                    proc = psutil.Process(pid)
                    if proc.name().lower() != name:
                        raise RuntimeError("PID 已被其他进程复用")
                    if created is not None and abs(proc.create_time() - float(created)) > 0.01:
                        raise RuntimeError("PID 已被其他进程复用")
                    current_exe = proc.exe()
                    if exe and current_exe and os.path.normcase(current_exe) != os.path.normcase(exe):
                        raise RuntimeError("进程映像已变化")
                    proc.kill()
                    proc.wait(timeout=5)
                    ok = True
                except Exception as exc:
                    self.failed.append(("进程 " + label, str(exc)))
            else:
                rc, _out, _err = run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=30)
                ok = rc == 0
            if ok:
                handled.append(label)
            elif not psutil:
                self.failed.append(("进程 " + label, "结束失败"))
        if handled and not self.dry_run:
            time.sleep(1.0)
        if handled:
            prefix = "[dry-run] 将结束进程: " if self.dry_run else "已结束进程: "
            self.log("  " + prefix + ", ".join(handled))
        else:
            self.log("  未能结束任何进程")
        return handled

    def stage_processes(self):
        self.status("结束 VMware 进程...")
        self.p(8, "> 阶段 2/9 结束 VMware 进程")
        found = self.discover_processes()
        if found:
            self.log("  发现进程: " + ", ".join(
                "%s(pid=%s)" % (name, pid) for pid, name, _exe, _created in found))
        self.kill_processes(found)
        self.p(16)

    # ---------------- 阶段：服务 / 驱动拆除
    def _teardown_one(self, name, phase):
        state = query_state(name)
        if state is None:
            self.log("  - %s: 未安装，跳过" % name)
            return
        label = "%s [%s]" % (name, phase)
        if self.dry_run:
            self.log("  [dry-run] 将停止 / 禁用 / 删除: " + label)
            return
        action = stop_service(name)
        if action in ("已停止", "本来就已停止", "已停止（服务项已消失）"):
            self.log("  %s -> %s" % (label, action))
        else:
            self.log("  ! %s -> %s（继续删除服务项）" % (label, action))
        disable_service(name)
        deleted = delete_service(name)
        left = query_state(name)
        if deleted and left is None:
            self.counters["drivers" if phase == "驱动" else "services"] += 1
            self.log("  已彻底删除: " + label)
        elif deleted:
            self.pending_services.append(name)
            self.log("  已标记删除，重启后从 SCM 消失: " + label)
        else:
            self.failed.append((label, "服务项删除失败"))
            self.log("  ! 服务项删除失败: " + label)

    def stage_services(self):
        self.status("拆除 VMware 服务与驱动...")
        self.p(26, "> 阶段 4/9 停止并删除服务 / 驱动")
        users, drivers = collect_vmware_services(self.log)
        driver_paths = []
        for name in drivers:
            path = driver_file_of_service(name)
            if path:
                driver_paths.append((name, path))
        for name in users:
            self._teardown_one(name, "服务")
        self.p(34)
        for name in drivers:
            self._teardown_one(name, "驱动")
        self.p(38)
        # 驱动服务项已删：先摘掉 PnP 设备节点，再删驱动映像，
        # 否则设备实例仍引用 vmusb.sys / vmnetadapter.sys，文件依旧删不掉
        self.disable_net_adapters()
        self.pnp_remove_devices()
        self.p(40)
        for _name, path in driver_paths:
            self.delete_file_item(path, "驱动映像")

    # ---------------- 文件
    def delete_file_item(self, path, label="文件"):
        if not path or not os.path.isabs(path):
            self.failed.append((str(path), "非绝对路径，跳过"))
            return False
        if is_vm_disk_path(path):
            self.notes.append("保护性跳过虚拟机文件: " + path)
            return False
        if not os.path.exists(path):
            return True
        if self.dry_run:
            self.log("  [dry-run] 将删除%s: %s" % (label, path))
            return True
        ok, reason = try_delete_file(path)
        if ok:
            self.counters["files"] += 1
            self.log("  已删除%s: %s" % (label, path))
            return True
        holders = holders_of(path)
        detail = ""
        if holders:
            detail = "；占用者: " + ", ".join(
                "%s(pid=%s%s)" % (app or "?", pid, "/" + svc if svc else "")
                for pid, app, svc in holders)
        elif label == "驱动映像":
            detail = "；内核映像仍在加载，需重启释放"
        self.pending_files.append(path)
        self.log("  ! 删除失败(%s)%s，加入待登记重启删除清单: %s" % (reason, detail, path))
        return False

    def collect_file_targets(self):
        env = os.environ
        dirs = []
        files = []

        def add_dir(path):
            if path and os.path.isabs(path) and not is_vm_disk_path(path):
                dirs.append(path)

        def add_file(path):
            if path and os.path.isabs(path):
                files.append(path)

        # Keep per-user and shared application-data roots untouched: they may contain
        # user configuration or virtual-machine data, even when named VMware.
        for base in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"),
                     env.get("ProgramW6432")):
            if base and os.path.isabs(base):
                add_dir(os.path.join(base, "VMware"))

        for base in (env.get("CommonProgramFiles"), env.get("CommonProgramFiles(x86)"),
                     env.get("CommonProgramW6432")):
            if base and os.path.isabs(base):
                add_dir(os.path.join(base, "VMware"))

        for base in (env.get("ProgramData"), env.get("APPDATA")):
            if base and os.path.isabs(base):
                add_dir(os.path.join(base, "Microsoft", "Windows", "Start Menu",
                                     "Programs", "VMware"))

        desktops = []
        userprofile = env.get("USERPROFILE") or ""
        public = env.get("PUBLIC") or ""
        if userprofile:
            desktops.append(os.path.join(userprofile, "Desktop"))
            desktops.append(os.path.join(userprofile, "OneDrive", "Desktop"))
        if public:
            desktops.append(os.path.join(public, "Desktop"))
        for desktop in desktops:
            for name in safe_listdir(desktop):
                if name.lower().endswith(".lnk") and "vmware" in name.lower():
                    add_file(os.path.join(desktop, name))

        system_root = env.get("SystemRoot") or r"C:\Windows"
        sysdir = os.path.join(system_root, "System32")
        wowdir = os.path.join(system_root, "SysWOW64")
        for name in SYSTEM32_FILES:
            add_file(os.path.join(sysdir, name))
            add_file(os.path.join(wowdir, name))
        for name in DRIVER_FILES:
            add_file(os.path.join(sysdir, "drivers", name))

        # DriverStore packages are managed only through pnputil; never recurse-delete
        # FileRepository directories directly.

        temp = env.get("TEMP") or ""
        for name in ("vminst.log", "vmmsi.log", "vmware-vminst.log"):
            add_file(os.path.join(temp, name))

        return unique_paths(dirs), unique_paths(files)

    def stage_files(self):
        self.status("删除安装目录与系统文件...")
        self.p(42, "> 阶段 5/9 删除文件与目录")
        dirs, files = self.collect_file_targets()
        self.log("  待处理目录 %d 个，文件 %d 个" % (len(dirs), len(files)))
        total = max(len(dirs) + len(files), 1)
        done = 0
        for path in dirs:
            done += 1
            if self.dry_run:
                if os.path.exists(path):
                    if os.path.isdir(path) and directory_contains_vm_artifacts(path):
                        self.log("  [保护] 目录含虚拟机文件或无法完整扫描，跳过: " + path)
                    else:
                        self.log("  [dry-run] 将删除目录: " + path)
            elif os.path.exists(path):
                ok, reason = try_delete_dir(path, self.pending_files.append)
                if ok:
                    self.counters["dirs"] += 1
                    self.log("  已删除目录: " + path)
                else:
                    if "虚拟机文件" in reason or "符号链接/联接点" in reason:
                        self.notes.append("保护性跳过目录: " + path + "（" + reason + "）")
                    self.log("  ! 目录未完全删除(%s): %s" % (reason, path))
            self.p(42 + 18.0 * done / total)
        for path in files:
            done += 1
            self.delete_file_item(path, "文件")
            self.p(42 + 18.0 * done / total)
        self.p(60)

    # ---------------- 注册表
    def _reg_delete(self, root, subkey, label):
        marker = (str(root), subkey.lower())
        if marker in self._seen_keys:
            return False
        self._seen_keys.add(marker)
        if not reg_exists(root, subkey):
            return False
        if self.dry_run:
            self.log("  [dry-run] 将删除注册表: %s > %s" % (ROOT_NAMES.get(root, root), subkey))
            return True
        ok, reason = delete_key_tree(root, subkey)
        if ok:
            self.counters["regkeys"] += 1
            self.log("  已删除注册表%s: %s > %s" % (label, ROOT_NAMES.get(root, root), subkey))
        else:
            self.failed.append(("%s > %s" % (ROOT_NAMES.get(root, root), subkey), reason))
            self.log("  ! 注册表删除失败(%s): %s > %s"
                     % (reason, ROOT_NAMES.get(root, root), subkey))
        return ok

    def core_registry_targets(self):
        return [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VMware, Inc."),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VMware, Inc."),
            (winreg.HKEY_CURRENT_USER, r"Software\VMware, Inc."),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\Applications\vmware.exe"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vmware.exe"),
        ]

    def stage_registry(self):
        self.status("清理注册表残留...")
        self.p(62, "> 阶段 6/9 清理注册表")

        for root, subkey in self.core_registry_targets():
            self._reg_delete(root, subkey, "项")

        # 厂商根键（含变体）
        for root, base in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
                           (winreg.HKEY_CURRENT_USER, "Software"),
                           (winreg.HKEY_CURRENT_USER, r"Software\WOW6432Node")):
            for name in enum_subkeys(root, base):
                if "vmware" in name.lower():
                    self._reg_delete(root, base + "\\" + name, "厂商项")
        self.p(66)

        # Only remove uninstall keys for which the matching official MSI uninstall
        # completed successfully; leave non-MSI/failed product registrations intact.
        for root, sub in self.msi_cleanup_targets:
            self._reg_delete(root, sub, "卸载项")

        # Windows Installer 的 Products/Features/UpgradeCodes 由 msiexec 管理；
        # 不手工删除这些数据库键，避免损坏其他 MSI 产品的注册关系。
        self.log("  - 保留 Windows Installer 内部数据库键，由官方卸载程序管理")
        self.p(70)

        # 文件关联 / ProgID（CLASSES 根不能直接枚举，走两个 Classes 视图）
        for root, base in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes"),
                           (winreg.HKEY_CURRENT_USER, r"Software\Classes")):
            for name in enum_subkeys(root, base):
                if name.lower().startswith("vmware"):
                    self._reg_delete(root, base + "\\" + name, "文件关联")

        # 残留服务项（动态扫一遍兜底）
        users, drivers = collect_vmware_services()
        for name in users + drivers:
            self._reg_delete(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Services" + "\\" + name, "服务项")
        self.p(76)

        # 虚拟网卡实例：Control\Class 与 Control\Network
        net_class = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
        for name in enum_subkeys(winreg.HKEY_LOCAL_MACHINE, net_class):
            if not name.isdigit():
                continue
            sub = net_class + "\\" + name
            values = reg_values(winreg.HKEY_LOCAL_MACHINE, sub)
            desc = str(values.get("DriverDesc", ("", 0))[0] or "")
            if "vmware" in desc.lower() or "vmnet" in desc.lower():
                self._reg_delete(winreg.HKEY_LOCAL_MACHINE, sub, "网卡实例")
        net_ctrl = r"SYSTEM\CurrentControlSet\Control\Network\{4d36e972-e325-11ce-bfc1-08002be10318}"
        for name in enum_subkeys(winreg.HKEY_LOCAL_MACHINE, net_ctrl):
            sub = net_ctrl + "\\" + name
            values = reg_values(winreg.HKEY_LOCAL_MACHINE, sub + "\\Connection")
            desc = str(values.get("Name", ("", 0))[0] or "")
            if desc.lower().startswith("vmnet") or "vmware" in desc.lower():
                self._reg_delete(winreg.HKEY_LOCAL_MACHINE, sub, "网卡绑定")
        self.p(80)

        # 网络位置（首次连接时生成的公共/专用配置）
        for root, base in (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles")):
            for name in enum_subkeys(root, base):
                sub = base + "\\" + name
                values = reg_values(root, sub)
                desc = str(values.get("Description", ("", 0))[0] or "")
                if "vmnet" in desc.lower() or "vmware" in desc.lower():
                    self._reg_delete(root, sub, "网络配置")

        # 服务事件日志源
        eventlog = r"SYSTEM\CurrentControlSet\Services\EventLog\Application"
        for name in enum_subkeys(winreg.HKEY_LOCAL_MACHINE, eventlog):
            if VMWARE_HINT.search(name):
                self._reg_delete(winreg.HKEY_LOCAL_MACHINE,
                                 eventlog + "\\" + name, "事件日志源")

        # SharedDLLs / MuiCache 里指向 VMware 的值
        shared = r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"
        for name, (data, _vtype) in reg_values(winreg.HKEY_LOCAL_MACHINE, shared).items():
            if isinstance(data, str) and "vmware" in data.lower():
                if self.dry_run:
                    self.log("  [dry-run] 将删除 SharedDLLs 值: " + name)
                elif delete_reg_value(winreg.HKEY_LOCAL_MACHINE, shared, name):
                    self.counters["regkeys"] += 1
                    self.log("  已删除 SharedDLLs 值: " + name)
        for root, base in (
                (winreg.HKEY_CURRENT_USER,
                 r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"),
                (winreg.HKEY_CURRENT_USER,
                 r"Local Settings\Software\Microsoft\Windows\Shell\MuiCache")):
            for name, (data, _vtype) in reg_values(root, base).items():
                text = (name + " " + str(data)).lower()
                if "vmware" in text and not self.dry_run:
                    delete_reg_value(root, base, name)

        # 启动项里指向 VMware 的值（保留本工具自己的接力任务）
        for root, base in (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")):
            for name, (data, _vtype) in reg_values(root, base).items():
                if name == RUNONCE_VALUE:
                    continue
                text = (name + " " + str(data)).lower()
                if "vmware" in text:
                    if self.dry_run:
                        self.log("  [dry-run] 将删除启动项: " + name)
                    elif delete_reg_value(root, base, name):
                        self.log("  已删除启动项: " + name)
        self.p(84)

    # ---------------- PnP 驱动包与设备节点
    def pnp_remove_inf(self):
        rc, out, _err = run(["pnputil", "/enum-drivers"], timeout=120)
        if rc != 0 or not out:
            self.log("  ! pnputil /enum-drivers 执行失败，跳过 OEM 驱动包清理")
            return
        chunks = re.split(r"(?mi)^\s*(?:Published Name|已发布名称)\s*[:：]\s*", out)
        for chunk in chunks[1:]:
            head = re.match(r"([^\r\n]+)", chunk)
            published = (head.group(1).strip() if head else "")
            if not published.lower().endswith(".inf"):
                continue
            original_match = re.search(r"(?mi)^\s*(?:Original Name|原始名称)\s*[:：]\s*([^\r\n]+)", chunk)
            provider_match = re.search(r"(?mi)^\s*(?:Provider Name|提供程序名称|提供商名称)\s*[:：]\s*([^\r\n]+)", chunk)
            original = (original_match.group(1).strip() if original_match else "").lower()
            provider = (provider_match.group(1).strip() if provider_match else "").lower()
            is_vmware = ("vmware" in provider) and original.startswith(
                ("vmnet", "vmusb", "vmx86", "vmci", "vsock", "hcmon", "vmparport", "vmware"))
            if not is_vmware:
                continue
            if self.dry_run:
                self.log("  [dry-run] 将删除 OEM 驱动包: %s (%s / %s)"
                         % (published, original, provider))
                continue
            rc2, out2, err2 = run(
                ["pnputil", "/delete-driver", published, "/uninstall", "/force"], timeout=180)
            if rc2 == 0:
                self.counters["inf"] += 1
                self.log("  已删除 OEM 驱动包: %s (%s)" % (published, original))
            else:
                self.failed.append(("OEM 驱动包 " + published, (err2 or out2 or "").strip()[:120]))
                self.log("  ! 删除 OEM 驱动包失败: %s" % published)

    def pnp_remove_devices(self):
        rc, out, _err = run(["pnputil", "/enum-devices"], timeout=180)
        if rc != 0 or not out:
            self.log("  ! pnputil /enum-devices 执行失败，跳过设备节点清理")
            return
        chunks = re.split(r"(?mi)^\s*(?:Instance ID|实例 ID)\s*[:：]\s*", out)
        targets = []
        for chunk in chunks[1:]:
            lines = chunk.splitlines()
            if not lines:
                continue
            instance = lines[0].strip()
            body = "\n".join(lines[1:])
            desc_match = re.search(r"(?mi)^\s*(?:Device Description|设备描述)\s*[:：]\s*([^\r\n]+)", body)
            desc = (desc_match.group(1).strip() if desc_match else "")
            upper = instance.upper()
            if (upper.startswith("ROOT\\VMWARE")
                    or upper.startswith("ROOT\\VMWVMCIHOSTDEV")
                    or "VID_0E0F&PID_0001" in upper
                    or "vmware" in desc.lower()
                    or "vmnet" in desc.lower()):
                targets.append((instance, desc))
        for instance, desc in targets:
            if self.dry_run:
                self.log("  [dry-run] 将移除设备节点: %s (%s)" % (instance, desc))
                continue
            rc2, out2, err2 = run(["pnputil", "/remove-device", instance], timeout=120)
            if rc2 == 0:
                self.counters["devices"] += 1
                self.log("  已移除设备节点: %s (%s)" % (instance, desc))
            else:
                self.failed.append(("设备节点 " + instance, (err2 or out2 or "").strip()[:120]))
                self.log("  ! 移除设备节点失败: %s" % instance)

    def disable_net_adapters(self):
        """先禁用 VMnet 虚拟网卡。设备处于 Started 状态时 pnputil /remove-device 会失败。"""
        if self.dry_run:
            self.log("  [dry-run] 将禁用 VMnet 虚拟网卡")
            return
        script = ("Get-NetAdapter -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.Name -like 'VMnet*' -or "
                  "$_.InterfaceDescription -like '*VMware*' } | "
                  "Disable-NetAdapter -Confirm:$false -ErrorAction SilentlyContinue")
        rc, _out, err = powershell(script, timeout=180)
        if rc == 0:
            self.log("  VMnet 虚拟网卡已禁用")
        else:
            self.log("  - 虚拟网卡禁用未完成（%s），继续尝试摘除设备" % (err or rc))

    def stage_pnp(self):
        self.status("清理 PnP 驱动包与虚拟设备...")
        self.p(86, "> 阶段 7/9 清理 PnP 驱动包与设备节点")
        self.pnp_remove_inf()
        self.p(90)

    # ---------------- 杂项
    def stage_misc(self):
        self.status("清理账户、规则与任务...")
        self.p(91, "> 阶段 8/9 清理账户 / 防火墙 / 计划任务")
        # These names are not standard Workstation/Player accounts. Preserve them;
        # removing an unrelated local account/group by name is too risky.
        self.notes.append("未自动删除 __vmware_user__ / __vmware__ 账户或组；如确认为本工具创建，请人工核验后处理")
        self.log("  - 账户/组名称仅作人工提示，未执行删除")

        if self.deep:
            if self.dry_run:
                self.log("  [dry-run] 将清理名称含 VMware 的防火墙规则与计划任务")
            else:
                script = ("Get-NetFirewallRule -ErrorAction SilentlyContinue | "
                          "Where-Object { $_.DisplayName -like '*VMware*' -or "
                          "$_.Name -like '*VMware*' } | Remove-NetFirewallRule "
                          "-ErrorAction SilentlyContinue")
                powershell(script, timeout=180)
                self.log("  防火墙规则已清理")
                rc, out, _err = run(["schtasks", "/query", "/fo", "csv", "/nh"], timeout=120)
                removed = 0
                for line in (out or "").splitlines():
                    cells = re.findall(r'"([^"]*)"', line)
                    if not cells:
                        continue
                    task = cells[0].lstrip("\\")
                    if "vmware" not in task.lower():
                        continue
                    rc2, _out2, _err2 = run(["schtasks", "/delete", "/tn", task, "/f"], timeout=60)
                    if rc2 == 0:
                        removed += 1
                self.log("  计划任务已清理 %d 个" % removed)

        hosts = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "drivers", "etc", "hosts")
        try:
            with open(hosts, "r", encoding="utf-8", errors="ignore") as handle:
                lines = handle.read().splitlines()
            hits = [line for line in lines
                    if "vmware" in line.lower() and not line.strip().startswith("#")]
            if hits:
                self.notes.append("hosts 文件里有 %d 条 VMware 相关记录，未自动修改，请人工确认" % len(hits))
                self.log("  ! hosts 内有 VMware 记录 %d 条（未改动）" % len(hits))
        except Exception:
            pass
        self.p(93)

    # ---------------- 收尾：重启接力
    def finalize(self):
        self.p(94, "> 阶段 9/9 登记重启删除队列并核验")
        if self.post_reboot and not self.dry_run:
            clear_post_reboot(self.log)
        if self.dry_run:
            self.log("  dry-run：未执行删除，也未登记重启删除队列")
            return
        if not self.pending_files:
            self.log("  没有需要重启后删除的文件")
            return
        unique = unique_paths(self.pending_files)
        self.log("  待重启删除文件 %d 个" % len(unique))
        added = add_pfro(unique)
        self.log("  已写入 PendingFileRenameOperations: %d 条" % added)
        if added != len(unique):
            self.failed.append(("PendingFileRenameOperations", "部分条目未写入"))
        self.queue_registration_complete = added == len(unique)
        schedule_post_reboot(self.log)

    # ---------------- 核验
    def stage_verify(self):
        self.status("核验残留...")
        self.p(96)
        items = []
        users, drivers = collect_vmware_services()
        service_label = "检测到服务: " if self.dry_run else "服务仍存在: "
        driver_label = "检测到驱动: " if self.dry_run else "驱动仍存在: "
        file_label = "检测到文件: " if self.dry_run else "文件仍在: "
        dir_label = "检测到目录: " if self.dry_run else "目录仍在: "
        registry_label = "检测到注册表: " if self.dry_run else "注册表仍在: "
        uninstall_label = "检测到卸载项: " if self.dry_run else "卸载项仍在: "
        process_label = "检测到进程: " if self.dry_run else "进程仍运行: "
        for name in users:
            items.append(service_label + name)
        for name in drivers:
            items.append(driver_label + name)
        dirs, files = self.collect_file_targets()
        for path in files:
            if os.path.exists(path):
                items.append(file_label + path)
        for path in dirs:
            if os.path.exists(path):
                if os.path.isdir(path) and directory_contains_vm_artifacts(path):
                    items.append("保护性跳过含虚拟机文件/不可完整扫描的目录: " + path)
                else:
                    items.append(dir_label + path)
        for root, subkey in self.core_registry_targets():
            if reg_exists(root, subkey):
                items.append(registry_label + "%s > %s" % (ROOT_NAMES.get(root, root), subkey))
        for entry in self.find_uninstall_entries():
            items.append(uninstall_label + (entry["display"] or entry["sub"]))
        procs = self.discover_processes()
        if procs:
            labels = ["%s(pid=%s)" % (name, pid) for pid, name, _exe, _created in procs]
            items.append(process_label + ", ".join(labels))
        if self.pending_files:
            items.append("已登记重启后删除的文件 %d 个" % len(unique_paths(self.pending_files)))
        self.remaining = items
        if items and self.dry_run:
            self.log("  dry-run 检测到 %d 项候选清理目标（本次未执行系统清理）" % len(items))
            for item in items:
                self.log("    - " + item)
        elif items:
            self.log("  核验结果：仍有 %d 项待处理" % len(items))
            for item in items:
                self.log("    - " + item)
        else:
            self.log("  核验结果：未发现残留")
        self.p(99)
        self.verification_complete = True

    # ---------------- 报告
    def write_report(self):
        if not self.log_path:
            return
        report_path = self.log_path.replace(".log", "_report.md")
        lines = []
        lines.append("# VMware Destroyer v%s 执行报告" % APP_VERSION)
        lines.append("")
        lines.append("- 时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("- 模式: %s" % ("仅检测" if self.dry_run else "实际清理"))
        lines.append("- 日志: %s" % self.log_path)
        if self.backup_dir:
            lines.append("- 注册表备份: %s" % self.backup_dir)
        lines.append("")
        lines.append("## 统计")
        lines.append("")
        lines.append("| 项目 | 数量 |")
        lines.append("| --- | --- |")
        lines.append("| 删除服务 | %d |" % self.counters["services"])
        lines.append("| 删除驱动 | %d |" % self.counters["drivers"])
        lines.append("| 删除文件 | %d |" % self.counters["files"])
        lines.append("| 删除目录 | %d |" % self.counters["dirs"])
        lines.append("| 删除注册表项/值 | %d |" % self.counters["regkeys"])
        lines.append("| 删除 OEM 驱动包 | %d |" % self.counters["inf"])
        lines.append("| 移除设备节点 | %d |" % self.counters["devices"])
        lines.append("")
        lines.append("## 待重启删除的文件")
        lines.append("")
        if self.pending_files:
            if not self.queue_registration_complete:
                lines.append("- 注意：重启删除队列未确认登记成功；下列文件只是待处理清单，不能认为重启后会自动删除。")
            for item in unique_paths(self.pending_files):
                lines.append("- %s" % item)
        else:
            lines.append("- 无")
        lines.append("")
        lines.append("## 失败项")
        lines.append("")
        if self.failed:
            for target, reason in self.failed:
                lines.append("- %s：%s" % (target, reason))
        else:
            lines.append("- 无")
        lines.append("")
        lines.append("## 只读检测结果（dry-run；未执行清理）" if self.dry_run else "## 剩余残留核验")
        lines.append("")
        if not self.verification_complete:
            lines.append("- 未完成残留核验，不能判断清理结果；不得视为无残留。")
        if self.remaining:
            for item in self.remaining:
                lines.append("- %s" % item)
        elif self.verification_complete:
            lines.append("- 未发现残留")
        lines.append("")
        lines.append("## 人工确认提示")
        lines.append("")
        if self.notes:
            for item in self.notes:
                lines.append("- %s" % item)
        else:
            lines.append("- 无")
        lines.append("")
        lines.append("> 虚拟机目录（.vmx / .vmdk 等）始终受保护，本工具不会触碰。")
        try:
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
            self.log("  报告已写入: %s" % report_path)
        except Exception as exc:
            self.log("  报告写入失败: %s" % exc)

    # ---------------- 主流程
    def run(self):
        try:
            self.prepare()
            if not self.post_reboot:
                if not self.precheck():
                    return 1
                self.make_restore_point()
                self.backup_registry()
                self.stage_processes()
                if self.run_msi:
                    self.stage_uninstall_msi()
            else:
                self.log("> 重启后接力模式：跳过预检与官方卸载")
                self.p(20)
            self.stage_services()
            self.stage_files()
            self.stage_registry()
            self.stage_pnp()
            self.stage_misc()
            self.finalize()
            self.stage_verify()
            self.p(100, "=== 流程执行完毕 ===")
            if self.dry_run:
                self.status("检测完成（只读；系统未清理）")
            elif self.pending_files or self.remaining:
                self.status("已完成，请重启计算机完成剩余清理")
            else:
                self.status("已完成，未发现残留")
            return 0
        except Exception as exc:
            self.failed.append(("流程中止", "%s: %s" % (type(exc).__name__, exc)))
            self.log("! 执行过程中出现未处理异常，流程中止：")
            for line in traceback.format_exc().splitlines():
                self.log("    " + line)
            self.status("执行中止（详见日志）")
            return 1
        finally:
            try:
                self.write_report()
            except Exception:
                pass


# ---------------------------------------------------------------- 日志适配
class ConsoleLogger(Logger):
    def __call__(self, message):
        try:
            print(str(message), flush=True)
        except Exception:
            pass
        Logger.__call__(self, message)


class QueueLogger(Logger):
    def __init__(self, sink):
        Logger.__init__(self)
        self.sink = sink

    def __call__(self, message):
        self.sink.put(("log", str(message)))
        Logger.__call__(self, message)


# ---------------------------------------------------------------- 图形界面
def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App:
        def __init__(self):
            self.root = tk.Tk()
            self.root.title("%s v%s" % (APP_NAME, APP_VERSION))
            self.ui = queue.Queue()
            self.worker = None
            self.engine = None
            self._build()
            self.root.after(60, self._drain)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        def _build(self):
            try:
                self.root.tk.call("tk", "scaling",
                                  self.root.winfo_fpixels("1i") / 72.0)
            except Exception:
                pass
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
            style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9),
                            foreground="#666666")
            style.configure("Warn.TLabel", font=("Microsoft YaHei UI", 9, "bold"),
                            foreground="#c62828")
            style.configure("Go.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=8)

            frame = ttk.Frame(self.root, padding=12)
            frame.pack(fill="both", expand=True)

            ttk.Label(frame, text="VMware 彻底卸载与残留清理", style="Title.TLabel").pack(anchor="w")
            ttk.Label(frame, style="Hint.TLabel",
                      text="将清理：服务/驱动(vmusb、vmx86、vmnet*)、文件、注册表、"
                           "PnP 驱动包与虚拟网卡、账户与规则。").pack(anchor="w", pady=(4, 2))
            ttk.Label(frame, style="Warn.TLabel",
                      text="虚拟机目录(.vmx/.vmdk)始终受保护；请先关闭所有虚拟机；完成后需重启。"
                      ).pack(anchor="w", pady=(0, 8))

            options = ttk.LabelFrame(frame, text="选项", padding=8)
            options.pack(fill="x")
            self.var_dry = tk.BooleanVar(value=bool(args.dry_run))
            self.var_deep = tk.BooleanVar(value=bool(args.deep))
            self.var_force = tk.BooleanVar(value=bool(args.force))
            self.var_rp = tk.BooleanVar(value=not args.no_restore_point)
            self.var_msi = tk.BooleanVar(value=not args.no_msi)
            ttk.Checkbutton(options, text="仅检测（只读扫描；日志和报告会写入输出目录）",
                            variable=self.var_dry).grid(row=0, column=0, sticky="w", padx=4)
            ttk.Checkbutton(options, text="深度清理（防火墙规则 / 计划任务）",
                            variable=self.var_deep).grid(row=0, column=1, sticky="w", padx=4)
            ttk.Checkbutton(options, text="强制（检测到运行中虚拟机也继续，风险高）",
                            variable=self.var_force).grid(row=1, column=0, sticky="w", padx=4)
            ttk.Checkbutton(options, text="先创建系统还原点",
                            variable=self.var_rp).grid(row=1, column=1, sticky="w", padx=4)
            ttk.Checkbutton(options, text="先执行官方卸载(msiexec /x)",
                            variable=self.var_msi).grid(row=2, column=0, sticky="w", padx=4)

            self.status_var = tk.StringVar(value="就绪")
            ttk.Label(frame, textvariable=self.status_var,
                      font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(10, 2))
            self.progress_var = tk.DoubleVar(value=0.0)
            ttk.Progressbar(frame, variable=self.progress_var, maximum=100.0
                            ).pack(fill="x", pady=(0, 8))

            log_frame = ttk.Frame(frame)
            log_frame.pack(fill="both", expand=True)
            scroll = ttk.Scrollbar(log_frame)
            scroll.pack(side="right", fill="y")
            self.log_text = tk.Text(log_frame, height=16, wrap="word",
                                    yscrollcommand=scroll.set,
                                    font=("Consolas", 9), bg="#111111", fg="#d6d6d6",
                                    insertbackground="#d6d6d6")
            self.log_text.pack(side="left", fill="both", expand=True)
            scroll.config(command=self.log_text.yview)
            self.log_text.configure(state="disabled")

            buttons = ttk.Frame(frame)
            buttons.pack(fill="x", pady=(8, 0))
            self.start_button = ttk.Button(buttons, text="开始清理", style="Go.TButton",
                                           command=self.start)
            self.start_button.pack(side="left")
            ttk.Button(buttons, text="打开日志目录",
                       command=lambda: self.open_path(self.engine.log_dir if self.engine
                                                      else args.log_dir or "")
                       ).pack(side="left", padx=6)
            ttk.Button(buttons, text="重启计算机", command=self.reboot).pack(side="right")
            ttk.Button(buttons, text="退出", command=self.on_close).pack(side="right", padx=6)

            self.root.minsize(760, 560)
            self.root.update_idletasks()
            width = min(920, self.root.winfo_screenwidth() - 80)
            height = min(720, self.root.winfo_screenheight() - 120)
            self.root.geometry("%dx%d+%d+%d" % (
                width, height, (self.root.winfo_screenwidth() - width) // 2,
                max((self.root.winfo_screenheight() - height) // 2 - 20, 0)))

        def open_path(self, path):
            if not path:
                return
            try:
                os.makedirs(path, exist_ok=True)
                os.startfile(path)  # noqa: S606
            except Exception as exc:
                messagebox.showwarning("打开目录", str(exc))

        def append(self, text):
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", text + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except Exception:
                pass

        def _drain(self):
            try:
                while True:
                    kind, payload = self.ui.get_nowait()
                    if kind == "log":
                        self.append(payload)
                    elif kind == "status":
                        self.status_var.set(payload)
                    elif kind == "progress":
                        self.progress_var.set(payload)
                    elif kind == "done":
                        self.finish(payload)
            except queue.Empty:
                pass
            except Exception:
                pass
            self.root.after(60, self._drain)

        def start(self):
            if self.worker and self.worker.is_alive():
                return
            dry = self.var_dry.get()
            if not dry and not is_admin():
                messagebox.showerror("权限不足", "请右键以管理员身份运行本程序。")
                return
            if not dry:
                if not messagebox.askyesno(
                        "确认",
                        "将删除 VMware 的服务、驱动、文件与注册表项。\n"
                        "虚拟机目录(.vmx/.vmdk)不会被动。\n\n继续吗？"):
                    return
            self.start_button.configure(state="disabled", text="执行中...")
            self.progress_var.set(0.0)
            self.status_var.set("正在执行...")

            engine = Engine(
                QueueLogger(self.ui),
                status=lambda text: self.ui.put(("status", text)),
                progress=lambda value: self.ui.put(("progress", value)),
                dry_run=self.var_dry.get(),
                deep=self.var_deep.get(),
                force=self.var_force.get(),
                restore_point=self.var_rp.get(),
                run_msi=self.var_msi.get(),
                log_dir=args.log_dir,
            )
            self.engine = engine

            def worker():
                code = 1
                try:
                    code = engine.run()
                except Exception:
                    self.ui.put(("log", "线程异常: " + traceback.format_exc()))
                finally:
                    self.ui.put(("done", code))

            self.worker = threading.Thread(target=worker, name="destroyer", daemon=True)
            self.worker.start()

        def finish(self, code):
            self.start_button.configure(state="normal", text="开始清理")
            if code == 0:
                self.status_var.set("完成。请重启计算机完成剩余清理。")
            else:
                self.status_var.set("未完全清理，请查看日志与报告。")
            if self.engine and self.engine.log_path:
                self.append("报告: " + self.engine.log_path.replace(".log", "_report.md"))

        def reboot(self):
            if not messagebox.askyesno("重启", "确定立即重启？未完成的删除需要重启才会生效。"):
                return
            run(["shutdown", "/r", "/t", "10"], timeout=20)

        def on_close(self):
            if self.worker and self.worker.is_alive():
                if not messagebox.askyesno("正在执行", "清理仍在进行，确定退出吗？"):
                    return
            try:
                self.root.destroy()
            except Exception:
                pass

    App().root.mainloop()
    return 0


# ---------------------------------------------------------------- 命令行
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="vmware_destroyer_v3.py",
        description="VMware Destroyer v%s -- 彻底卸载 VMware 并清理文件/服务/注册表/PnP 残留"
                    % APP_VERSION)
    parser.add_argument("--check", "--dry-run", dest="dry_run", action="store_true",
                        help="只检测并出报告，不做任何改动")
    parser.add_argument("--silent", "-q", action="store_true", help="静默执行，无界面")
    parser.add_argument("--no-gui", dest="no_gui", action="store_true", help="等同 --silent")
    parser.add_argument("--force", action="store_true", help="检测到运行中虚拟机也继续执行")
    parser.add_argument("--deep", action="store_true", help="额外清理防火墙规则与计划任务")
    parser.add_argument("--no-restore-point", dest="no_restore_point", action="store_true",
                        help="不创建系统还原点")
    parser.add_argument("--no-msi", dest="no_msi", action="store_true",
                        help="跳过 msiexec 官方卸载，直接清残留")
    parser.add_argument("--post-reboot", dest="post_reboot", action="store_true",
                        help="重启后接力模式（由 RunOnce 自动调用）")
    parser.add_argument("--log-dir", dest="log_dir", default=None, help="日志与报告输出目录")
    return parser.parse_args(argv)


def run_console(args):
    logger = ConsoleLogger()
    engine = Engine(
        logger,
        status=lambda text: print("[状态] " + str(text), flush=True),
        progress=lambda value: None,
        dry_run=args.dry_run,
        deep=args.deep,
        force=args.force,
        restore_point=not args.no_restore_point,
        run_msi=not args.no_msi,
        post_reboot=args.post_reboot,
        log_dir=args.log_dir,
    )
    code = engine.run()
    print("")
    print("退出码: %d" % code)
    if engine.log_path:
        print("日志: %s" % engine.log_path)
        print("报告: %s" % engine.log_path.replace(".log", "_report.md"))
    return code


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    headless = args.silent or args.no_gui or args.post_reboot

    if not is_admin() and not args.dry_run:
        if headless:
            print("需要管理员权限。请右键选择“以管理员身份运行”，或在管理员终端中执行。")
            return 2
        if elevate():
            return 0
        print("需要管理员权限。请右键选择“以管理员身份运行”。")
        return 2

    if headless:
        return run_console(args)
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
