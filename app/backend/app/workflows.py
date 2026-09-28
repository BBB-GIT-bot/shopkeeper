"""Business validation and immutable review context."""
from datetime import date, timedelta
from .db import parse_json, money_value
from fastapi import HTTPException

def fail(message):
    raise HTTPException(422,{'code':'business_validation','message':message})

def validate_proposal(c, product_id, round_id, data):
    try:
        first=date.fromisoformat(data['date_from']); last=date.fromisoformat(data['date_to'])
    except (KeyError,ValueError,TypeError): fail('投放周期必须为有效日期')
    if first>last: fail('周期开始日期不能晚于结束日期')
    if not c.execute('SELECT 1 FROM listings WHERE product_id=? AND shop_id=?',(product_id,data['shop_id'])).fetchone(): fail('商品未关联该店铺')
    materials=data.get('material_ids',[])
    if not isinstance(materials,list) or not materials or len(set(materials))!=len(materials): fail('请选择不重复的可用素材')
    for material_id in materials:
        material=c.execute('SELECT * FROM materials WHERE id=?',(material_id,)).fetchone()
        if not material or material['product_id']!=product_id or material['round_id']!=round_id or material['review_status']!='usable' or material['archived_at']: fail('素材须属于当前商品轮次、审核可用且未归档')
    targets=data.get('targets',{})
    for key in ('ctr','conversion_rate','roi'):
        value=targets.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or value<0 or value!=value or value==float('inf'): fail(f'{key} 目标必须为非负数')
        if key!='roi' and value>1: fail(f'{key} 目标以 0—1 比例填写')
    bounce=targets.get('detail_bounce_rate')
    if bounce is not None and (not isinstance(bounce,(int,float)) or isinstance(bounce,bool) or not 0<=bounce<=1): fail('跳失目标以 0—1 比例填写')
    strategy=data.get('strategy',{})
    for key in ('target_audience','audience_packages'):
        if not isinstance(strategy.get(key),list) or not strategy[key] or any(not isinstance(x,str) or not x.strip() for x in strategy[key]): fail(f'{key} 必须为非空文字数组')
    if not isinstance(strategy.get('description'),str) or not strategy['description'].strip(): fail('策略说明不能为空')
    for key in ('bid','budget'):
        if money_value(strategy.get(key),key)<0: fail('出价与预算不能为负')
    groups=data.get('groups',[])
    if not isinstance(groups,list) or len(groups)<2: fail('至少需要两个 A/B 实验组')
    codes=[]
    for group in groups:
        if not isinstance(group,dict) or not isinstance(group.get('group_code'),str) or not group['group_code'].strip(): fail('实验组编码不能为空')
        codes.append(group['group_code'])
        if not group.get('material_ids') or any(x not in materials for x in group['material_ids']): fail('实验组只能引用本建议已选素材')
        if not isinstance(group.get('audience'),dict): fail('实验组须有定向人群')
        for key in ('bid','budget'):
            if money_value(group.get(key),key)<0: fail('实验组出价与预算不能为负')
        if not isinstance(group.get('targets'),dict): fail('实验组须有指标目标')
    if len(set(codes))!=len(codes): fail('实验组编码不能重复')

def validate_metric(c, experiment_id, data):
    experiment=c.execute('SELECT * FROM experiments WHERE id=?',(experiment_id,)).fetchone()
    if not experiment or experiment['status']!='approved': fail('实验不存在或尚未批准')
    if not experiment['date_from']<=str(data['date'])<=experiment['date_to']: fail('日期不在实验周期')
    proposal=c.execute('SELECT groups_data FROM proposals WHERE id=?',(experiment['proposal_id'],)).fetchone()
    if data['group_code'] not in {g['group_code'] for g in parse_json(proposal[0],[])}: fail('实验组不属于当前实验')
    if (data.get('detail_visits') is None)!=(data.get('detail_bounces') is None): fail('详情页访问与跳失须成对填写')
    if data.get('detail_bounces') is not None and data['detail_bounces']>data['detail_visits']: fail('跳失次数不能超过访问次数')
    return experiment

def validate_report(sections,actions):
    required=('product_overview','diagnosis_summary','content_assets','experiment_record','performance_review')
    if not isinstance(sections,dict) or any(not isinstance(sections.get(k),dict) or not isinstance(sections[k].get('summary'),str) or not sections[k]['summary'].strip() for k in required): fail('复盘须包含五部分及各部分摘要')
    performance=sections['performance_review']
    if any(not isinstance(performance.get(key),list) for key in ('facts','hypotheses','evidence_gaps')): fail('复盘须区分已验证指标、归因推测和证据缺口')
    if not isinstance(actions,list) or not actions: fail('至少保留一项优化行动')
    ids=[]
    for action in actions:
        if not isinstance(action,dict) or any(not isinstance(action.get(key),str) or not action[key].strip() for key in ('action_id','description','evidence','verification_method')): fail('优化行动须包含编码、说明、依据和验证方法')
        ids.append(action['action_id'])
    if len(set(ids))!=len(ids): fail('优化行动编码不能重复')

def report_context(c, exp, rows, aggregate, scope):
    proposal=c.execute('SELECT * FROM proposals WHERE id=?',(exp['proposal_id'],)).fetchone()
    product=c.execute('SELECT * FROM products WHERE id=?',(exp['product_id'],)).fetchone()
    approval=c.execute("SELECT * FROM approvals WHERE proposal_id=? AND decision='approved' ORDER BY id DESC LIMIT 1",(proposal['id'],)).fetchone()
    saved=parse_json(proposal.get('content_snapshot'),{}) or {}
    contents=[saved[k] for k in ('diagnosis','creative') if k in saved]
    competitors=[dict(x) for x in c.execute("SELECT * FROM competitors WHERE product_id=? AND verification_status='verified'",(product['id'],))]
    dates={x['date'] for x in rows}; first=date.fromisoformat(scope['date_from']); last=date.fromisoformat(scope['date_to'])
    missing=[str(first+timedelta(days=i)) for i in range((last-first).days+1) if str(first+timedelta(days=i)) not in dates]
    return {'product':saved.get('product',dict(product)),'proposal':dict(proposal),'approval':dict(approval) if approval else None,'experiment':dict(exp),'contents':contents,'competitors':competitors,'data_scope':scope,'data_rows':rows,'actual_metrics':aggregate['metrics'],'totals':aggregate['totals'],'target_snapshot':parse_json(exp['targets'],{}),'data_completeness':{'missing_dates':missing,'missing_detail_rows':sum(x.get('detail_visits') is None for x in rows)}}

def demo_report(context):
    metrics=context['actual_metrics']; target=context['target_snapshot']
    facts=[]
    for key,title in [('ctr','CTR'),('conversion_rate','转化率'),('roi','广告投入产出比'),('detail_bounce_rate','详情页跳失率')]:
        value=metrics[key]
        facts.append(f'{title}不可计算' if value is None else f'{title}为 {value:.4f}，目标 {target.get(key)}')
    gaps=[]
    if metrics['detail_bounce_rate'] is None: gaps.append('详情页数据缺失或分母为零，无法判断跳失情况')
    if context['data_completeness']['missing_dates']: gaps.append('部分业务日期没有经营数据')
    sections={
        'product_overview':{'summary':f"教学商品：{context['product']['name']}，类别 {context['product']['category']}，基准价 {context['product']['base_price']} 元",'product_id':context['product']['id']},
        'diagnosis_summary':{'summary':'引用本轮已确认诊断，客群、卖点及价格分析见内容快照'},
        'content_assets':{'summary':'本轮已确认主图与视频创意及已审核的教学素材','material_ids':parse_json(context['proposal']['material_ids'],[])},
        'experiment_record':{'summary':f"课堂人工模拟实验 EXP-{context['experiment']['id']}，审批意见 {context['approval']['comment'] if context['approval'] else ''}，执行备注 {context['experiment']['execution_note']}",'approval':context['approval']},
        'performance_review':{'summary':'程序计算经营指标；下面的原因属于待验证推测','facts':facts,'hypotheses':['面料信息表达可能影响转化，需独立实验验证'],'evidence_gaps':gaps},
    }
    return {'sections':sections,'actions':[{'action_id':'fabric-detail','description':'补充面料特写及尺码说明','evidence':'结合经营指标及诊断提出，尚未验证因果','verification_method':'保持价格及人群一致，对比优化前后详情页数据'}]}
