# VMware Uninstall Clean

用于 Windows 上 VMware Workstation / Player 卸载及残留清理的 Python 工具，支持图形界面、命令行、仅检测模式和 Markdown 报告。

- 脚本：[`vmware_uninstall_clean.py`](./vmware_uninstall_clean.py)
- 当前脚本版本：`3.1.1`
- 界面和日志中的程序名仍为 **VMware Destroyer**。

> **警告：实际清理会修改系统。** 工具会结束目标进程，停止、禁用及删除目标服务/驱动，并清理文件、注册表及 PnP 设备。使用前请关闭全部虚拟机并备份重要数据。它不是 VMware / Broadcom 官方卸载工具，也不能保证适用于所有安装版本或清除全部残留。
>
> **默认不是仅检测模式。** 不带参数启动会打开实际清理模式的界面；`--silent` 也会实际清理，不会再弹出 GUI 确认。首次使用请明确添加 `--check`。

## 环境要求

- Windows；代码依赖 Windows 注册表、服务管理及系统命令，不适用于 Linux / macOS。
- Python 3.8 或更高版本，建议使用 64 位 Python。
- 图形界面需要安装 Python 时包含 Tcl/Tk（`tkinter`）。
- 实际清理需要管理员权限；仅检测可使用普通权限，但部分受限内容可能无法读取。
- 使用系统自带的 PowerShell、`sc.exe`、`pnputil.exe`、`msiexec.exe` 等工具。

第三方包不是必需项，但建议安装，以改善进程识别和服务控制：

```powershell
python -m pip install psutil pywin32
```

缺少可选包时，脚本会使用系统命令作为替代。不同 Windows 版本、语言和权限配置可能影响检测与清理结果。

## 获取脚本

```powershell
git clone https://github.com/Ghostlikes/vmware-uninstall-clean.git
cd vmware-uninstall-clean
```

也可以直接下载仓库中的 `vmware_uninstall_clean.py`。

## 推荐使用流程

1. 备份虚拟机及重要宿主机数据，完全关闭虚拟机，不要仅挂起。
2. 建议优先尝试 Windows“已安装的应用”中的官方卸载功能。
3. 先运行仅检测模式，查看日志和报告中的目标范围。
4. 确认后，在管理员终端中执行实际清理。
5. 若报告提示需要重启，保存工作后重启，再重新检测。不要只凭退出码判断是否清理完成。

### 1. 仅检测（推荐首先执行）

控制台模式：

```powershell
python vmware_uninstall_clean.py --check --silent
```

图形界面，预先选中“仅检测”：

```powershell
python vmware_uninstall_clean.py --check
```

仅检测不会执行目标删除、停止服务、创建还原点或登记重启删除任务，但仍会写入日志和报告。检测到目标仍存在是正常结果，不表示执行清理失败。

### 2. 图形界面实际清理

```powershell
python vmware_uninstall_clean.py
```

非管理员启动时会尝试请求 UAC 提权。点击“开始清理”前，请确认“仅检测”选项和其他开关的状态。

### 3. 控制台实际清理

在**管理员 PowerShell / 终端**中执行：

```powershell
python vmware_uninstall_clean.py --silent
```

默认会尝试创建系统还原点、备份部分注册表项，并调用检测到的 Workstation / Player MSI 卸载程序，然后清理残留。还原点和注册表导出不保证成功，也不是完整系统备份。

### 4. 深度清理

```powershell
python vmware_uninstall_clean.py --silent --deep
```

额外清理名称匹配 VMware 的防火墙规则和计划任务。**当前这部分主要按名称筛选，不是完整的所有权验证；装有其他 VMware 产品或自定义同名规则/任务时，应避免启用。**

### 5. 指定输出目录

```powershell
python vmware_uninstall_clean.py --check --silent --log-dir "D:\VMwareCleanLogs"
```

## 参数说明

| 参数 | 作用 |
| --- | --- |
| `--check` / `--dry-run` | 仅检测，不执行系统清理；仍输出日志和报告。 |
| `--silent` / `-q` | 控制台执行，不打开 GUI；仍打印日志，不等于仅检测。 |
| `--no-gui` | 等同于 `--silent`。 |
| `--deep` | 额外处理防火墙规则与计划任务。 |
| `--no-restore-point` | 跳过创建系统还原点。 |
| `--no-msi` | 跳过官方 MSI 卸载，直接清理残留；不会因此变成仅检测。 |
| `--log-dir PATH` | 自定义日志、报告和备份输出目录。 |
| `--force` | 在检测到运行中的虚拟机时仍继续；可能造成数据损坏或系统故障，不建议使用。 |
| `--post-reboot` | 内部重启接力入口；不建议手动调用，会跳过常规预检和官方卸载。 |
| `--help` | 显示命令行帮助。帮助中的程序名目前可能仍显示旧文件名。 |

## 清理范围与保护边界

当前实现包含以下处理：

- 按精确进程名称筛选 VMware 进程；可选 `psutil` 路径会检查 PID 身份，而不是按路径包含 `vmware` 就结束 Python 等进程。
- 枚举已知服务及符合限定驱动映像路径的驱动；显式保护列出的 Hyper-V 等系统组件。
- 官方 MSI 卸载项限定到 Workstation / Player，不按 VMware 发布者名称卸载全部产品。
- 清理安装目录、部分系统文件、注册表项目、设备节点及符合筛选条件的 OEM 驱动包。
- 驱动包通过 `pnputil` 处理，不直接递归删除 `DriverStore\FileRepository`。
- 递归删除目录前检查已知虚拟机文件扩展名；发现这些文件或扫描不完整时保护整个目标目录，并对符号链接/联接点采取保护措施。
- 不自动删除 `__vmware_user__` / `__vmware__` 账户或组，仅给出人工提示。

**保护机制不等于完整的数据安全保证。** 它依赖已知名称、路径和文件类型，不能识别所有自定义数据布局。部分目录和注册表清理仍涉及共享的 VMware 名称空间；安装了其他 VMware 产品时，不应直接使用实际清理模式。请先人工审查目标。

## 日志、报告与重启

默认输出到：

```text
C:\ProgramData\VMwareDestroyer\logs\
├── destroy_YYYYMMDD_HHMMSS.log
├── destroy_YYYYMMDD_HHMMSS_report.md
└── backup_YYYYMMDD_HHMMSS\
```

实际位置取决于 `ProgramData`、`--log-dir` 和目录写入权限；默认目录不可写时会尝试使用临时目录。仅检测不创建注册表备份目录。

查看报告时请区分：

- **检测目标**：仅检测模式发现的对象，不是已删除的对象。
- **待处理文件**：内存中的待登记清单，不代表已经成功写入重启删除队列。
- **队列登记结果**：实际清理末尾尝试写入 `PendingFileRenameOperations` 并注册 RunOnce 接力；必须查看是否成功。
- **失败项 / 未完成核验**：异常中止或核验未完成时，不能推断为“无残留”。

文件访问被拒绝可能与驱动占用、权限或其他保护机制有关，不能仅凭 `WinError 5` 确定原因。若流程在队列登记前中止，不能认为重启后会自动删除这些文件。

RunOnce 接力依赖管理员登录、可用权限、原脚本与 Python 路径；服务单独处于“待删除”状态时，当前实现不保证注册自动接力。重启后应再次检测，不保证自动清理完成。

## 退出码与验证范围

| 退出码 | 含义 |
| --- | --- |
| `0` | 流程正常返回；不保证没有失败项或残留。GUI 启动/退出也不能用该码判断清理结果。 |
| `1` | 引擎预检中止或发生未处理异常等情况。 |
| `2` | 缺少实际清理所需的管理员权限，或提权未完成。 |

当前版本完成了静态语法检查及 Windows 注册表 API 调用参数签名检查；**未完成覆盖全部功能的实机回归测试**。残留核验也没有完整覆盖所有 PnP、规则、任务和注册表类别，报告中的“未发现残留”仅限当前检查范围。

`3.1.1` 修复了 `winreg.DeleteKey` 参数错误，并增加异常失败记录、核验完成状态及重启队列登记状态提示。请以实际日志和人工检查为准。

## 反馈问题

请通过 [Issues](https://github.com/Ghostlikes/vmware-uninstall-clean/issues) 提供 Windows / Python / VMware 版本、运行参数、完整异常堆栈和相关报告。上传前请移除用户名、内部路径、虚拟机名称及其他敏感信息。不要提交密码、访问令牌或 SSH 私钥。
