# 测试

改动录制或识别逻辑后，至少跑一遍 `test_auto_detect.py`（不需要 SAI2）。另外两个要在真机上跑。

| 文件 | 需要 SAI2 | 需要开画布 | 干什么 |
|---|---|---|---|
| `test_auto_detect.py` | 否 | 否 | 参数校验、缓存合并、外部表往返、超时保护 |
| `test_detect_live.py` | 是 | **是** | 三层识别：假 MD5 → 摘掉已知 offset → 逼结构扫描 |
| `test_record_live.py` | 是 | **是** | 落笔录制，确认「一落笔就停」没有复发 |

```bat
venv\Scripts\python.exe tests\test_auto_detect.py
venv\Scripts\python.exe tests\test_detect_live.py
venv\Scripts\python.exe tests\test_record_live.py
```

## 前置条件

**SAI2 里必须开着一张画。** 没有真画布时画布链表只剩占位节点，`get_canvas_list()` 会把它 `[1:]` 掉，结果是空 —— 任何 offset 都验证不了。这是嗅探器报告里最常见的失败原因，不是参数找错了。

`test_record_live.py` 会**在画布上画一条短线**，测完立刻发 `Ctrl+Z` 撤销。跑之前确认这张画可以随便动。

## SAI2 没开着怎么办

两个测试脚本会尝试启动 SAI2，但需要你告诉它装在哪：

```bat
set SAI2_EXE=D:\你的路径\sai2.exe
```

不设置的话脚本只会在「已有 sai2.exe 进程」的情况下继续。

## 跑失败了先看什么

1. 画布链有几个节点？打印出来的是 `画布链 1 个: '便签薄'(...)` 就说明没有真画布。
2. `test_detect_live.py` 第 3 层会打印前置检查：已知偏移池里还有没有本机那个 offset。如果有，第 3 层走的是「已知偏移」而不是结构扫描，不算验证了兜底路径。
3. `test_record_live.py` 的帧目录打在临时目录（`%TEMP%\sai2-rec-test-*`），日志也在那里，跑完不会污染项目目录。

## 不写缓存

`test_detect_live.py` 调 `ad.detect(..., save=False)`，不会往 `%USERPROFILE%\.art-timelapse-versions.json` 里写测试条目。`test_record_live.py` 会临时改写主程序配置并在结束时还原。
