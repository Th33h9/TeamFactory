# TeamFactory0713 Oracle Repair Pipeline

这个 stage 专门检查并修复 TeamFactory0713 中 canonical solution 无法取得
`1.0` reward 的 instance。它不会让模型盲扫所有数据：规则 Oracle probe 先筛选，
只有失败项才进入 Claude Code 修复池。

主 TeamFactory pipeline 现在会在 Stage3 后默认以单 instance 模式调用同一套
流程。修复结果通过 `responsible_stage` 标记问题属于 `agent1`、`stage2_ast`
或 `agent2_stage3`；只有事务式 canonical recheck 达到 `1.0` 才会保留修改。
独立批处理入口仍可用于重审存量 dataset。

```text
TeamFactory0713 instances
          |
          v
 OracleProbe x10 -------- 1.0 --------> oracle_pass
          |
       < 1.0 / error
          v
 OracleRepair x10 (CC + Sonnet 4.6)
          |
          v
 IntegrityValidation
          |
          v
 TransactionalCommit x4
          |
          v
 OracleRecheck -------- 1.0 ---------> repaired
          |
       still failing
          +----> OracleRepair next round
                       |
                  rollback on failure
```

## 安全边界

- `environment/start.md`、`environment/api_manifest.json`、`instruction.md`
  必须逐字节保持不变。
- 已有 oracle 测试源码不可删除或改写，测试数量和命令不可减少。
- 拒绝 skip/xfail、无条件成功、伪造 reward、暴露 `/solution` 或测试答案。
- instance 和远端 image tar 使用事务提交；Oracle recheck 未达到 `1.0` 时回滚。
- 每个 Agent round、Oracle result、验证结果和状态均保存在独立 run 目录。

## 启动

全量运行：

```bash
cd /volume/pt-coder/users/kka/TeamFactory
./scripts/run_oracle_repair.sh \
  --run-name TeamFactory0713-oracle-repair-sonnet46-w10
```

先做 20 条 smoke：

```bash
./scripts/run_oracle_repair.sh \
  --run-name TeamFactory0713-oracle-repair-smoke20 \
  --limit 20
```

只规划和检查参数，不启动容器或模型：

```bash
./scripts/run_oracle_repair.sh --limit 20 --plan-only
```

默认并发为 Oracle probe 10、Claude Code repair 10、事务提交/recheck 4。
可分别通过 `--oracle-workers`、`--repair-workers` 和
`--finalize-workers` 调整。默认最多进行两轮修复；第二轮会收到第一轮 recheck
的失败证据。

运行产物位于：

```text
/volume/pt-coder/users/kka/Hyperdistill/runs/<run-name>/
  run_config.json
  summary.json
  cases.jsonl
  oracle_results.jsonl
  repair_trajectories.jsonl
  oracle_jobs/
  items/<instance>/
    state.json
    oracle/
    evidence/
    repair_result.round-N.json
    validated_repair.round-N.json
```
