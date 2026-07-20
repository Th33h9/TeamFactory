# API 覆盖检查问题记录

记录时间：2026-07-15

## 问题结论

当前 TeamFactory 的 API coverage gate 确实会执行，但它可能产生假通过。现有检查并不等价于“测试依赖的全部项目 API 都已在 `start.md` 中得到完整说明”，而更接近于：部分被测试直接导入的符号，其名称是否出现在 `api_manifest.json` 或 `start.md` 的任意位置。

因此，部分 instance 虽然 coverage report 显示通过，`start.md` 仍可能缺少测试实际依赖的类、函数、方法、签名或行为契约。

## 已确认的问题

### 1. `src/` 布局的模块名归一化错误

Stage 2 会将 `src/waveresponse/_core.py` 登记为 `src.waveresponse._core`，而测试实际导入的是 `waveresponse._core`。两者无法匹配，导致测试导入被误判为非项目符号。

已发现的假通过实例包括：

| 项目 | 测试数量 | 识别出的必需 API 数量 | 检查结果 |
| --- | ---: | ---: | --- |
| inline-snapshot | 306 | 0 | passed |
| waveresponse | 375 | 0 | passed |
| mathtypejx | 78 | 0 | passed |
| univers | 401 | 0 | passed |

相关代码：`teamfactory/stages/stage2_ast/stage.py` 中的 `module_name_for`、`project_module_inventory` 和 `is_repo_module`。

### 2. 没有解析通过模块别名访问的具体符号

当前收集器主要处理 `ast.Import` 和 `ast.ImportFrom`。例如测试代码：

```python
import advanced_navigation.anpp_packets.an_packet_7

advanced_navigation.anpp_packets.an_packet_7.FileTransferFirstPacket(...)
```

检查器只记录模块 `advanced_navigation.anpp_packets.an_packet_7`，不会继续解析实际使用的 `FileTransferFirstPacket`。因此，即使 `start.md` 漏掉或错误描述这个类，coverage report 仍可能显示通过。

### 3. `api_manifest.json` 可以替代 `start.md` 覆盖

当前 `symbol_is_covered()` 的逻辑是：符号出现在 `api_manifest.json` 或 `start.md` 中，任一条件满足即可通过。

但做题 agent 实际依赖的是 `/workspace/start.md`，不会使用 instance 中的 `environment/api_manifest.json`。如果符号只存在于 manifest，验证器会通过，但做题 agent 仍然得不到对应 API 信息。

相关代码：`teamfactory/stages/agent2_stage3/stage.py` 中的 coverage report 与 `symbol_is_covered()`。

### 4. 覆盖检查仅验证名称出现，不验证内容质量

当前检查允许使用完整名称、最后一个名称片段或最后两个名称片段进行正则文本匹配。名称在 `start.md` 任意位置出现就可能算作覆盖，但没有确认：

- API 是否位于 `Core API` 章节；
- 模块路径和归属是否正确；
- 函数签名或类定义是否完整；
- 类的关键变量、方法及签名是否齐全；
- 参数、返回值、异常和行为契约是否足够；
- 名称是否仅偶然出现在示例或普通文本中。

### 5. 相对导入会被跳过

测试中的相对导入目前会因为 `node.level` 被直接跳过，可能继续遗漏 `conftest.py`、测试辅助模块或包内测试所依赖的项目符号。

## 影响

1. coverage report 的 `passed=true` 不能证明 `start.md` API 完整。
2. `required_api_symbol_count=0` 可能是识别失败，而不是测试没有使用项目 API。
3. Agent2 回流机制不会修复未被 coverage collector 发现的 API。
4. 已生成的 TeamFactory instance 可能包含同类假阳性，需要在修复检查器后重新审计。

## 建议修复方案

### Stage 2：建立可靠的测试依赖 API 清单

1. 从 `pyproject.toml`、`setup.cfg`、`setup.py` 和实际包目录识别 package root。
2. 对 `src/`、`lib/` 等源码布局去掉容器目录前缀，生成规范模块名。
3. 建立测试文件的 import alias 表，解析 `ast.Name` 和 `ast.Attribute` 访问链。
4. 收集测试调用、实例化、继承、装饰器和类型注解实际引用的项目符号。
5. 纳入 `conftest.py`、fixture 和可解析的相对导入。
6. 将符号映射回源码 AST 中的规范类、函数或方法定义。

### Agent2 coverage gate：只接受可供做题 agent 使用的说明

1. `api_manifest.json` 只作为审计索引，不能独立满足覆盖条件。
2. 每个必需符号必须在 `start.md` 的 `Core API` 中存在对应条目。
3. 检查规范模块路径、类或函数名称以及公开签名，而不只是短名称文本匹配。
4. 类 API 应检查测试需要的关键方法和成员，而不是只检查类名。
5. 覆盖失败时回流 Agent2，重写后重新运行同一套检查。

### 必须增加的硬性保护

当项目包含测试，且测试中存在项目包导入，但 `required_api_symbol_count == 0` 时，必须判定 coverage extraction 异常并失败，不能将其视为 100% 覆盖。

## 修复后的验收标准

- `src/` 布局项目能识别正确的公开模块名。
- 模块导入后的属性调用可以解析到具体类、函数或方法。
- manifest 中有、但 `start.md` Core API 中没有的符号必须检查失败。
- 仅在普通文本中提到短名称不能算作完整覆盖。
- coverage report 同时给出规范符号、来源测试、对应源码定义和 `start.md` 条目。
- 使用上述已确认的项目作为回归样例，不再出现“数百个测试、必需 API 为 0、检查仍通过”的情况。

## 现有证据位置

- Stage 2 实现：`teamfactory/stages/stage2_ast/stage.py`
- Agent2 coverage gate：`teamfactory/stages/agent2_stage3/stage.py`
- 运行中间产物：`.work/<run>/items/<instance>/api_coverage_report.json`
- 数据集产物：`/volume/pt-coder/users/kka/harbor/datasets/TeamFactory0713/`

在上述问题修复并完成存量重审之前，不应把现有 coverage report 的通过状态解释为 API 内容完整。
