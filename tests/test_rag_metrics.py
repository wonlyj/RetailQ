import pytest

from retailq.knowledge.evaluation.metrics import retrieval_metrics, faithfulness


def test_metrics_use_rank_and_distinct_evidence_groups():
    result = retrieval_metrics([9, 4, 7], [{4}, {7}], ks=(1, 3))
    assert result.recall == {1: 0, 3: 1}
    assert result.mrr == 0.5


def test_equivalent_chunks_do_not_count_group_twice():
    result = retrieval_metrics([4, 5, 9], [{4, 5}, {7}], ks=(1, 3))
    assert result.recall == {1: 0.5, 3: 0.5}
    assert result.mrr == 1
    assert retrieval_metrics([], [{4}], ks=(1,)).mrr == 0
    assert retrieval_metrics([4], [], ks=(1,)).mrr is None


def test_mrr_is_truncated_to_top_ten():
    assert retrieval_metrics(list(range(1,12)),[{11}]).mrr==0
    assert retrieval_metrics(list(range(1,11)),[{10}]).mrr==.1


def test_multi_evidence_coverage_uses_rankings_before_refusal():
    from retailq.knowledge.evaluation.report import _aggregate
    rows=[{'phase':'complete','status':'refused','multi_evidence_full_coverage':0.0,'evidence_coverage':None},
          {'phase':'complete','status':'answered','multi_evidence_full_coverage':1.0,'evidence_coverage':1.0},
          {'phase':'complete','status':'answered','multi_evidence_full_coverage':None,'evidence_coverage':1.0}]
    assert _aggregate(rows)['full_evidence_coverage']==.5


def test_faithfulness_is_na_for_no_claims_and_counts_only_support():
    assert faithfulness([]) is None
    assert faithfulness([True, False, True]) == pytest.approx(2/3)


def test_report_excludes_refusals_from_faithfulness_and_groups_by_bucket():
    from retailq.knowledge.evaluation.report import build_report
    rows=[{'eval_id':'A01','bucket':'A_policy','difficulty':'easy','strategy':'dense','phase':'complete',
           'status':'answered','metrics':{'recall':{'1':1.0,'3':1.0},'mrr':1.0},
           'claims':[{'supported':True},{'supported':False}],'faithfulness':.5,'elapsed_seconds':1.0,
           'candidate50_metrics':{'recall':{'50':1.0}},'evidence_coverage':1.0,'multi_evidence_full_coverage':1.0},
          {'eval_id':'D01','bucket':'D_missing','difficulty':'hard','strategy':'dense','phase':'complete',
           'status':'refused','metrics':{'recall':{'1':None,'3':None},'mrr':None},
           'claims':[],'faithfulness':None,'elapsed_seconds':2.0,'latency_incomplete':True}]
    report=build_report(rows,split='test',expected=8)
    dense=report['overall']['dense']
    assert dense['recall']['1']==1 and dense['faithfulness_macro']==.5 and dense['faithfulness_micro']==.5
    assert dense['answered']==1 and dense['refused']==1 and dense['judge_claims']==2
    assert dense['recall_at_50']==1.0 and dense['full_evidence_coverage']==1.0
    assert dense['latency_mean_seconds']==1.0 and dense['latency_samples']==1
    assert report['complete'] is False and report['by_bucket']['D_missing']['dense']['faithfulness_macro'] is None


def test_report_rejects_unknown_strategy():
    from retailq.knowledge.evaluation.report import build_report
    with pytest.raises(ValueError):
        build_report([{'eval_id':'A01','strategy':'other'}],split='test',expected=1)
