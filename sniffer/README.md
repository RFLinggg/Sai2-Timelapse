# Sai2-Sniffer

读取你机器上 SAI2 的版本参数，产出一份可以发回来的报告。

Sai2-Timelapse 靠 exe 的 MD5 精确匹配版本表。表里没有的版本，程序会套用你下拉里
选的"差不多"的版本 —— 它不会报错，但录出来的帧可能是空的或者花屏，日志里一个错都没有。
这个工具就是为了把那种情况摊开来：算出版本参数，并且**真的读一张画布存成 PNG**，
让作者一眼看出参数对不对。

---

## 一、怎么用（普通用户）

**前提：SAI2 必须开着，而且里面要打开至少一个画布。** 没有打开的文档就读不到东西。

1. 双击 `Sai2-Sniffer.exe`
2. 窗口上方会显示它找到的 SAI2 路径、版本号、MD5
3. 点 **开始扫描**，等 2~10 秒
4. 看到"适配参数：session_offset = 0x…"就说明成功了
5. 点 **保存报告（zip）**，把那个 zip 发给作者

如果扫描失败，日志里会写原因。最常见的是「没有打开的文档」。

### 报告里有什么

| 文件 | 内容 |
|---|---|
| `report.json` | 完整参数：exe 路径 / MD5 / 版本资源 / session_offset / 布局 / 画布尺寸 |
| `canvas.png` | 一张画布缩略图。**这张图是判断参数对不对的唯一证据** |
| `说明.txt` | 人看的摘要 |

`canvas.png` 里是你在 SAI2 里画的画面。如果内容不方便外传，发之前把它从 zip 里删掉，
作者看 `report.json` 也能干活，只是要多问几句。

### 为什么杀软可能报毒

这个工具要读 SAI2 进程的内存（不读内存就算不出参数），加上程序没有数字签名。
「读取其他进程内存 + 未签名」这个组合在 Windows Defender 眼里和外挂很像。
源码全在这个目录里，可以自己看。程序不联网，不写注册表，不改 SAI2 的任何数据。

---

## 二、给维护者（技术说明）

### 为什么不能只读版本号

同一个版本号对应多个不同编译：`2024.05.22` 至少有日文原版和 [Saier] 汉化版，
MD5 不同、`session_offset` 也不同。所以：

- **版本资源**（`FileDescription` / `ProductVersion`）只给人看
- **MD5** 才是 `sai_api_lookup` 的键
- **session_offset** 才是能不能录的决定因素

### 探测原理

不用字符串锚点。上游的 `probe/auto_detect.py` 靠搜「便签薄」这个占位画布名反查，
日文版 / 英文版 / 精简版就没有这个词，直接失效。

这里的做法是结构扫描：

1. 按页批量读 `sai2.exe` 模块映像（5.9MB，约 0.1s），用 numpy 一次解析成 u64 数组
2. 筛出值落在可读堆区的槽
3. 对每个候选：节点地址必须**页对齐**、`+width/+height` 是合理正整数、
   `next_canvas` 是 0 或合法指针、`+name` 能解码成可打印 UTF-16
4. 打分排序。权重最高的一条是「**链表头是不带扩展名的占位画布**」——
   因为主程序 `get_canvas_list()` 正是用 `[1:]` 丢掉第一个节点，
   选错 offset 的直接后果是画布下拉里少一个文档，不崩、不报错，很难发现
5. 逐个候选按主程序同一套 `SAIv2_API_Base` 造类，真读一张画布。
   读得出来才算通过 —— 这样"嗅探器能读"就等价于"主程序能录"

字符串锚点法保留着，只在结构扫描连一个多节点链表都没找到时才跑（它每个锚点都要
全内存搜一遍，本机 9 个锚点要 35 秒，不能放在正常路径上）。

### 报告怎么用

`report.json` 里的 `class_snippet` 可以直接粘进 `src/art_timelapse/sai.py`：

```python
@register_sai_api
class SAIv2_API_918afa18(SAIv2_API_Base):
    version_name = 'PaintTool SAI Ver.2 (64bit) Preview.2024.05.22'
    exe_hash = '918afa18b2e67f29a800b52154b8f14d'
    session_offset = 0x321620
```

`layout` 是 `new`（2026.09.25 那种大重构）时，片段里会附带 `SAICanvas._fields_`
覆盖 —— 那种版本光有 offset 读不出图，字段偏移也得换。

先看 `canvas.png`：画面正常 = 参数对；花屏 / 全灰 / 全黑 = 布局变了，光有 offset 不够。

### 自检

```
# 源码版，直接出报告（我验收用）
venv\Scripts\python.exe sniffer\sniffer.py --cli --out probe\sniff_out

# 打包版（无控制台，输出同时写 cli.log）
dist\Sai2-Sniffer\Sai2-Sniffer.exe --cli --out probe\sniff_exe

# 启动即扫一次，方便截图核对界面（自检钩子）
dist\Sai2-Sniffer\Sai2-Sniffer.exe --autoscan
```

打包：

```
venv\Scripts\python.exe -m PyInstaller sniffer\Sai2-Sniffer.spec --noconfirm --distpath dist --workpath build/sniffer
```

故意出文件夹版不做单文件：单文件每次启动要解压到 `%TEMP%`，而且
「自解压 + 读内存 + 无签名」在杀软眼里更像外挂。

### 已知局限

- **只支持 SAI2**（`sai2.exe`）。`sai.exe` 是 32 位 SAI1，布局完全不同，会明确报不支持
- **必须有打开的文档**，否则画布链表为空，任何方法都无从下手
- **只解决 offset，不保证布局**。`layout` 只有 `base` / `new` 两种候选，
  遇到第三种结构（字段又挪了）会全部验证失败并如实报错
- 扫描要读 6MB 模块映像，杀软实时防护可能让耗时从 2 秒变成十几秒
- 报告里的 `canvas.png` 是用户的画面内容，注意隐私

### 文件位置

```
sniffer/
    sniffer.py            主程序（GUI + CLI 一体）
    Sai2-Sniffer.spec     PyInstaller 配置
    README.md             本文件
probe/
    shot_sniffer.py       打包版截图探针（--arg=--autoscan 可拍扫描后的状态）
```

## 三、反馈

加 QQ 群 **942992768**（主程序底栏也能一键复制群号）。发报告时顺带说一句
你装的 SAI2 是什么版本、从哪下的，能省一轮往返。
