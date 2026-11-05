# 第四章混合检索运行指南（验收进行中）

第四章使用 Milvus Standalone 的中文分析器和原生 BM25、BGE-M3 dense 向量、RRF 融合及 `BAAI/bge-reranker-v2-m3`。MySQL 保存知识内容、会话和低置信度问题；来源快照保存在知识目录。Milvus Lite 不支持本章的全文检索，不能作为本章的服务端替代。

以下演示库建库命令的参数已核对；为保留用户现有演示数据，本次没有在演示库上完整执行这组命令。已验证的隔离测试环境及人工测试命令见桌面 `RetailQ第四章人工测试指南.md`，真实四策略结果见[评估说明](ch04-evaluation.md)。不要删除现有 MySQL 或 Milvus 卷来重建知识库。

## 本机配置

Python 3.12 环境安装 `requirements-dev.lock`，启动 Docker Desktop；在现有 `.env` 中配置对话模型、MySQL、独立嵌入服务和重排服务。`MILVUS_URI` 指向 Standalone（默认 `http://127.0.0.1:19530`），`KNOWLEDGE_ENABLED=true`，`RERANK_MODEL=BAAI/bge-reranker-v2-m3`。重排密钥可单独配置；只有明确允许复用时，才将 `RERANK_REUSE_EMBEDDING_KEY=true`。不要把密钥写入仓库或评估报告。

```powershell
cd D:\AI-Agent\RetailQ
$ProjectPython = '.\.venv\Scripts\python.exe'
docker compose -p retailq -f compose.yaml up -d --wait mysql milvus
& $ProjectPython -m scripts.knowledge init-db --chapter 4 --scope demo
& $ProjectPython -m scripts.knowledge prepare-tokenizer
$CorpusDraft = (& $ProjectPython -m scripts.knowledge prepare-corpus --manifest examples/knowledge/manifest.json) | ConvertFrom-Json
& $ProjectPython -m scripts.knowledge build-index --build-id $CorpusDraft.build_id
& $ProjectPython -m scripts.knowledge publish-index --build-id $CorpusDraft.build_id
& $ProjectPython -m scripts.knowledge query --text 'MH-LP100的适用体重是多少？' --strategy bm25
```

这些命令操作当前 `.env` 指向的演示环境；先确认连接地址、知识目录和现有数据。来源冲突或未知的旧知识会阻止导入，需要先核查，不能通过删除卷绕过。语料更新生成独立集合，验证完成后才发布活动版本；保留归档供旧引用定位。

## 校准和评估

使用新目录保存每一轮，不覆盖或删除旧轮次。模型调用可能产生费用；中断前已发送但未记录的请求在恢复时可能再次计费。以下命令仅供复现旧评估，旧校准方法不适合作为新门槛；新相关性标注流程见[校准说明](ch04-calibration.md)。

```powershell
$RunStamp = Get-Date -Format yyyyMMdd-HHmmss
$CalibrationDir = "artifacts/evals/ch04/calibration-$RunStamp"
$TestDir = "artifacts/evals/ch04/test-$RunStamp"
& $ProjectPython -m scripts.evaluate_rag run --split calibration --mode real --output $CalibrationDir
& $ProjectPython -m scripts.calibrate_rag --legacy --report "$CalibrationDir/report.json" --output artifacts/knowledge/rag-confidence.json
& $ProjectPython -m scripts.evaluate_rag run --split test --mode real --output $TestDir
```

上面 `--legacy` 仅用于复现历史实验，旧方法可能将相关但不可答的候选误判为不相关，尚在验证的新流程见 [校准说明](ch04-calibration.md)。20 个历史校准问题用于各策略分别选取分数门槛，分数不是概率；80 个测试问题用于四策略独立对比，不应用测试结果调整门槛。每轮绑定模型、语料、代码、依赖和策略身份，身份变化后不能将旧 checkpoint 接到新轮次。报告区分 Recall、MRR、Faithfulness、拒答和无可用分母的 N/A；Faithfulness 由模型裁判评估，仍需人工抽查。

服务中断后，身份未变化时可用 `python -m scripts.evaluate_rag resume --output <原目录>` 恢复，查看报告用 `report --output <原目录>`；确认不再继续该轮时，可用 `seal --output <原目录> --reason '<原因>'` 封存。保留错误记录，不以未完成报告声称最终结果。更多门槛说明见 [校准说明](ch04-calibration.md)。

## 聊天与来源

```powershell
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000 --workers 2
```

访问 `http://127.0.0.1:8000/`；回答中的编号可打开来源片段和章节，并查看归档原文。每条完整回答的满意度选择只存储在本浏览器的 sessionStorage，选定后锁定，不上传服务端。证据不足时明确拒答并记录问题。历史归档缺失或损坏时应保留现场并核查，不能用当前文档冒充旧版本。
