# Sai2-Timelapse

给 PaintTool SAI / SAI2 用的缩时回放录制工具。直接读 SAI 进程里的画布位图，所以录出来是整幅画布——没有缩放、没有旋转、没有窗口遮挡。

> **本项目是 [cromachina/art-timelapse](https://github.com/cromachina/art-timelapse) 的衍生版本，遵循 GPL-3.0。**
> 界面层（WebView 前端）、SAI2 版本表、三层自动适配、嗅探器都是在本仓库里加的，原始读取内核与录制循环来自上游。
> 上游作者：**cromachina**。LICENSE 全文见 [LICENSE](LICENSE)。

当前版本 **v1.30**，内置 26 个 SAI 构建（24 个 SAI2 + 2 个 SAI1）。

---

## 和上游有什么不同

| | 上游 art-timelapse | 本仓库 |
|---|---|---|
| 界面 | Tk | Tk + **WebView 双界面**（默认 WebView，可切回 Tk） |
| SAI2 版本 | 少数几个 | **24 个**，2019.08 → 2026.10.19 |
| 认不出的版本 | 用不了 | **三层自动适配**：MD5 → 已知偏移试探 → 内存结构扫描 |
| 诊断 | 无 | **Sai2-Sniffer** 独立嗅探器，出报告给维护者 |
| 语言 | 英文 | 中 / 英 / 日 |
| 打包 | 单文件 exe | 文件夹版 + 一键安全模式启动 |

上游的读取内核、tile 解码、`VideoSequenceWriter` 分段录制逻辑都原样保留，只做了 bug 修复和扩展。

## 下载

到 [Releases](../../releases) 下载 `Sai2-Timelapse_vX.Y_win64.zip`，解压即用，不需要装 Python。

系统要求：Windows 10 / 11 64 位。

## 从源码跑

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set PYTHONPATH=src
venv\Scripts\python.exe -m art_timelapse
```

## 自己构建

需要 PyInstaller。构建分两趟：先出文件夹版，再出单文件版（单文件版不进发布包，启动慢、杀软误报高）。

```bat
set ONEFILE=0
venv\Scripts\python.exe -m PyInstaller packaging\Sai2-Timelapse.spec --noconfirm --distpath dist_new --workpath build\onedir-new

venv\Scripts\python.exe -m PyInstaller packaging\Sai2-Timelapse.spec --noconfirm
```

打发布包用 `tools\repack_release.py`，它会把安全模式启动脚本和使用说明一起塞进 zip。

发版前跑一遍自检，它会真启动一次程序，核对窗口标题、版本表条目数、说明书标题、PE 版本资源四处一致：

```bat
venv\Scripts\python.exe tools\check_build.py
```

## 遇到「你的 SAI2 版本程序不认识」

先在 SAI2 里**打开一张画**，再启动本程序。v1.27 起会自动探测：

1. 查内置版本表（按 exe 的 MD5）
2. 按已知的 `(offset, 布局)` 组合逐个真读一张画验证
3. 都不行就扫内存结构，找画布链表指针

成功后会写进 `%USERPROFILE%\.art-timelapse-versions.json`，下次启动直接命中。

都不行的话，用 [Sai2-Sniffer](sniffer/) 出报告发 issue——报告里有 exe 指纹、候选偏移、画布缩略图，够定位。

## 维护者文档

- [交接说明.md](交接说明.md) —— 适配一个 SAI2 新版本的完整流程、命令速查、真实踩过的坑
- [适配说明.md](适配说明.md) —— 逐版本记录每次适配的验证数据（画布尺寸、缩放比、mean/std、画面内容）
- `tools\inspect_report.py` / `tools\add_version.py` —— 从嗅探器报告到版本表条目，各一条命令

### 加一个版本的流程

```bat
:: 1. 看报告
venv\Scripts\python.exe tools\inspect_report.py "D:\path\report.zip"

:: 2. 写进版本表（--apply 才落盘，顺带抬版本号）
venv\Scripts\python.exe tools\add_version.py --report "D:\path\report.zip" --apply

:: 3. 自检
venv\Scripts\python.exe tools\check_build.py
```

**两条最容易踩的**：

- **std 低不等于读错。** 极淡的线稿在白底上 std 能压到 4 出头，看着像空白。判据是缩略图里线条是否连贯，不是统计量。
- **offset 不能按日期外推。** 2024.01.06 是 `0x320220`，比 2024.02.22 的 `0x320420` 更低。只能实测。

## 已知限制

- 只支持 Windows。上游的 Linux / Wayland 路径（Xwayland + Bottles）在本仓库没有测过。
- 2019 年初的两个构建（`da5b98fa`、`5cfa944b`）结构不同，无法定位画布链表，不支持。
- `new` 布局（2026.09 之后的大重构）的自动适配在真机上只验证过 base 布局，new 布局是照已有字段表推导的。

## 许可证

GPL-3.0，与上游一致。详见 [LICENSE](LICENSE)。

修改过的文件保留原版权声明；每个被改动的源文件在文件头标注了改动内容与日期。
