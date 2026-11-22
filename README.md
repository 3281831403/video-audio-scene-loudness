# 视频场景响度匹配

## 题面

把整部片按场景做响度匹配后，对白场景还是忽大忽小：分块不重叠，短场景测不准；门限用固定 -20 LUFS，静音段把整体响度拉低；增益差值不钳制，最大可到 ±60 dB；应用后不检查削顶，产生失真；交叉淡化不保持长度，时间轴错位；异常检测用绝对阈值，不按目标判断；场景重叠不报错，重复处理；空样本不校验，返回 -inf；批量共用块缓冲，轨与轨之间串音；`block_ms=0` 不校验，直接除零。sceneloud/match.py 是核心，tests/test_match.py 15个红。要修的是：分块重叠并用相对门限，增益限制在 ±24 dB 内，应用后防削顶，交叉淡化保持长度，异常检测相对目标，校验重叠与空样本，隔离每轨缓冲，并拒绝 block_ms=0。

## 运行

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始状态下应恰好得到 `15 failed`，并且没有 collection、import 或 syntax error。实现 `sceneloud/match.py` 后，同一命令应得到 `15 passed`。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 约束

- `tests/test_match.py` 不可修改；只能修改 `sceneloud/match.py` 等实现文件。
- 只使用 Python 标准库与 pytest，离线运行，不使用随机数，不调用 sleep。
- 场景统一使用 `{"start", "end", "samples"}` 结构，`start`/`end` 为秒，`samples` 为单声道 PCM。
- 响度单位统一为 LUFS，场景边界为 `[start, end)`，数值比较容差为 0.1 LU。
