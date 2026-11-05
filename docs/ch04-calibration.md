# 第四章置信度校准

2026-11-05 引入首位候选相关性单独标注的新校准流程；此前的可回答性校准可能对口语问题产生高误拒，历史结果与配置保留供对照。小样本结果与局限见[评估说明](ch04-evaluation.md)。旧命令必须显式加 `--legacy` 才能复现历史方法；不要将该方法当作新门槛的推荐依据。

采集器 `python -m scripts.evaluate_relevance --output <新目录>` 只允许本机隔离 3308/19531 及指定知识目录，采集前验证来源章节和活动语料摘要；人工核对每条首位候选是否与问题相关，填写候选 ID、布尔标签与理由，并绑定完整 checkpoint 的规范化 JSON SHA-256。然后运行 `python -m scripts.calibrate_rag --report <checkpoint.json> --annotations <annotations.json> --output <新策略文件>`。缺项、错配、无正负两类或记录摘要变化均拒绝拟合；来源章节存在不等于自动证明预期事实，仍需人工审核。校准和测试问题应按问题族分开，保留具体数据、运行身份和逐题判定。

标注文件的 `identity` 必须原样复制 checkpoint 的 `identity`；`labels` 是逐条标注数组，每条为 `{"eval_id":"K01","strategy":"dense","candidate_id":3,"relevant":true,"reason":"候选回答所问的清洗规则"}`。无候选时 `candidate_id` 为 `null`、`relevant` 为 `false`，理由仍须填写。`checkpoint_sha256` 对解析后的 checkpoint 用 `json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')` 计算 SHA-256，并非文件原始字节的哈希。每种策略都必须覆盖冻结的全部校准问题。

校准集包含 20 个标注问题，覆盖政策、型号、口语、知识缺失及多证据类型；测试集另有 80 个问题。每条按 dense、BM25、hybrid、hybrid_rerank 四种策略评估，不能用校准集成绩替代独立测试成绩。

对每种策略分别使用其实际分数尺度，新流程依据首位候选的人工相关性标注选取平衡准确率最高的门槛；旧流程依据可回答性及必需证据覆盖情况，易混淆相关性和充分性。分数不是经过概率校准的置信概率。门槛过高会错拒可答问题，过低可能引入无关证据，应结合各桶召回、作答和拒答数量审阅。

语料、模型、Prompt、查询规则或配置身份变化后，需要使用匹配当前身份的校准结果。校准和测试使用独立的新目录，失败及恢复情况保留在对应目录。`report.json` 包含样本分母和身份，`report.md` 提供汇总；完整性和效果是不同结论，且自动 Faithfulness 裁判不能替代人工事实核验。
