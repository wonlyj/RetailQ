def test_malformed_router_output_stays_in_parse_rate_denominator():
    from scripts.evaluate_router import summarize
    rows=[{'observations':{'x':{'raw_content':'{}','phase':'parsed'}},'ok':True,'review':{}},
          {'observations':{'x':{'raw_content':'{}','phase':'parsed'}},'ok':True,'review':{}},
          {'observations':{'x':{'raw_content':'{bad','phase':'invalid'}},'ok':False,'review':{}}]
    result=summarize({'cases':rows,'expected_count':3})
    assert result['json_received']==3 and result['json_parsed']==2
    assert result['json_parse_rate']==2/3 and result['status']=='fail'


def test_unreviewed_semantics_cannot_pass():
    from scripts.evaluate_router import summarize
    assert summarize({'cases':[{'ok':True,'observations':{},'review':{}}],'expected_count':1})['status']=='pending'
