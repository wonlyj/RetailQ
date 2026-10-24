from copy import deepcopy


def actual_faq(found=False, keyword='邮费'):
    return {'text':'演示政策未查到。','finish_reason':'stop',
            'tool_calls':[{'name':'query_faq','args':{'keyword':keyword},
                           'result':{'ok':True,'data':{'found':found,'items':[{'answer':'演示规则'}] if found else []}}}]}


def test_expected_faq_miss_requires_literal_keyword_and_zero_hits():
    from scripts.evaluate_tools import score_tool_case
    case={'expected_tool':'query_faq','expected_args':{'keyword':'邮费'},'expected_found':False}
    assert all(score_tool_case(actual_faq(),case).values())
    assert not all(score_tool_case(actual_faq(True),case).values())
    assert not all(score_tool_case(actual_faq(False,'运费'),case).values())


def test_correct_tool_wrong_argument_or_missing_ticket_is_failure():
    from scripts.evaluate_tools import score_tool_case
    actual={'text':'演示订单1001已发货','finish_reason':'stop','tool_calls':[{'name':'query_order',
        'args':{'order_id':'1001'},'result':{'ok':True,'data':{'source':'demo','order_id':'1001','status':'已发货'}}}]}
    case={'expected_tool':'query_order','expected_args':{'order_id':'001001'}}
    assert not all(score_tool_case(actual,case).values())
    ticket={'text':'已创建T1','finish_reason':'stop','tool_calls':[{'name':'create_ticket','args':{'ticket_type':'投诉'},
            'result':{'ok':True,'data':{'ticket_no':'T1','status':'待处理'}}}],'persisted_ticket_nos':[]}
    assert not all(score_tool_case(ticket,{'expected_tool':'create_ticket'}).values())
    ticket['persisted_ticket_nos']=['T1']
    assert all(score_tool_case(ticket,{'expected_tool':'create_ticket'}).values())


def test_manifest_rejects_missing_duplicate_and_unreviewed():
    from scripts.evaluate_tools import summarize_report
    report={'expected_case_ids':['a','b'],'run_completed':False,'cases':[]}
    assert summarize_report(report)['status']!='pass'
    passed={'id':'a','auto_checks':{'tools':True},'review':{'status':'pass','reason':'核对实际输出'}}
    report.update(run_completed=True,cases=[passed,deepcopy(passed)])
    assert summarize_report(report)['status']!='pass'
