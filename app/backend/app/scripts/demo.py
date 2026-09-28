"""Seed a complete labelled classroom scenario through the real business APIs."""
import csv
import io
import json
import os
import time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.config import storage_root
from app.worker.main import Worker

def create_demo(client):
    def login(role):
        password=os.getenv('DEMO_OPERATOR_PASSWORD' if role=='operator' else 'DEMO_MANAGER_PASSWORD',role+'123')
        result=client.post('/api/auth/login',json={'username':role,'password':password})
        assert result.status_code==200,result.text
        return {'Authorization':'Bearer '+result.json()['access_token']}
    operator=login('operator'); manager=login('manager')
    def call(path,body=None,method='POST',role='operator',status=200):
        result=client.request(method,'/api'+path,json=body,headers=operator if role=='operator' else manager)
        assert result.status_code==status,result.text
        return result.json()
    existing=call('/products?keyword=SUN-001',method='GET')['items']
    if any(x['product_code']=='SUN-001' for x in existing):
        return {'status':'existing','message':'SUN-001 已存在，保留现有记录，不重复建立课堂场景'}
    shops=[]
    known=call('/shops',method='GET')['items']
    for code,platform in [('SHOP-A','淘宝（模拟）'),('SHOP-B','抖音（模拟）')]:
        shop=next((x for x in known if x['shop_code']==code),None) or call('/shops',{'shop_code':code,'platform':platform,'name':platform+'教学店','platform_shop_id':'DEMO-'+code},status=201)
        shops.append(shop)
    product=call('/products',{'product_code':'SUN-001','name':'夏季凉感防晒衣（教学示例）','category':'服装','base_price':'299.00','attributes':{'用途':'课堂演示','面料':'轻薄透气，实际性能需实测'}},status=201)
    skus=[]
    for color,label in [('BLUE','蓝'),('GRAY','灰')]:
        for size in ['S','M']:
            skus.append(call(f"/products/{product['id']}/skus",{'sku_code':f'{color}-{size}','attributes':{'color':label,'size':size}},status=201))
    call(f"/products/{product['id']}",{'main_sku_id':skus[0]['id']},method='PATCH')
    for shop in shops:
        listing=call(f"/products/{product['id']}/listings",{'shop_id':shop['id'],'platform_product_id':shop['shop_code']+'-SUN'},status=201)
        for i,sku in enumerate(skus):
            call(f"/listings/{listing['id']}/sku-mappings",{'sku_id':sku['id'],'platform_sku_id':shop['shop_code']+'-'+sku['sku_code'],'sale_price':'299.00','stock_quantity':8 if i==0 else 50,'warning_threshold':10},status=201)
    competitor=call(f"/products/{product['id']}/competitors",{'competitor_code':'COMP-1','name':'竞品防晒衣（教学资料）','price':'269.00','selling_points':['透气','多规格'],'source_url':'https://example.invalid/classroom'},status=201)
    call(f"/competitors/{competitor['id']}",{'verification_status':'verified'},method='PATCH')
    first=call(f"/products/{product['id']}/rounds",status=201)
    diagnosis=call(f"/rounds/{first['id']}/diagnoses/generate",{'demo_mode':True},status=201)
    call(f"/contents/{diagnosis['id']}/confirm",{'expected_version':diagnosis['version']})
    creative=call(f"/rounds/{first['id']}/creatives/generate",{'demo_mode':True,'diagnosis_content_id':diagnosis['id']},status=201)
    creative['content'].update(selected_image_ids=['image-1','image-2'],selected_video_ids=['video-1'])
    creative=call(f"/contents/{creative['id']}",{'expected_version':creative['version'],'content':creative['content']},method='PATCH',status=201)
    call(f"/contents/{creative['id']}/confirm",{'expected_version':creative['version']})
    original_queue=call('/settings/queue',method='GET',role='manager')
    call('/settings/queue',{'execution_timeout_seconds':4,'heartbeat_interval_seconds':1,'max_auto_retries':1},method='PATCH',role='manager')
    def task(kind,selection,scenario):
        return call('/tasks',{'creative_content_id':creative['id'],'selection_id':selection,'task_type':kind,'demo_scenario':scenario},status=202)
    jobs=[task('image','image-1','fail_once'),task('image','image-2','success'),task('video','video-1','success'),task('image','image-1','always_fail'),task('video','video-1','timeout')]
    cancelled=task('image','image-1','late_result');call(f"/tasks/{cancelled['id']}/cancel",{})
    worker=Worker()
    try:
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            worker.tick()
            states=[call(f"/tasks/{job['id']}",method='GET') for job in jobs]
            if all(x['terminal'] for x in states):break
            time.sleep(.15)
        assert [x['status'] for x in states]==['succeeded','succeeded','succeeded','failed','timeout'],states
    finally:
        worker.close()
        call('/settings/queue',{k:original_queue[k] for k in ('concurrency','execution_timeout_seconds','max_auto_retries','poll_interval_seconds','heartbeat_interval_seconds','dispatch_interval_seconds')},method='PATCH',role='manager')
    materials=call(f"/materials?product_id={product['id']}&round_id={first['id']}",method='GET')['items']
    for material in materials:
        call(f"/materials/{material['id']}/review",{'decision':'usable','comment':'课堂演示素材核验通过'})
        call(f"/materials/{material['id']}/tags",{'tags':['演示素材','凉感']},method='PATCH')
    targets={'ctr':.022,'conversion_rate':.03,'roi':5,'detail_bounce_rate':.4}
    proposal=call(f"/rounds/{first['id']}/ad-proposals/generate",{'shop_id':shops[0]['id'],'diagnosis_content_id':diagnosis['id'],'creative_content_id':creative['id'],'material_ids':[x['id'] for x in materials],'date_from':'2026-09-21','date_to':'2026-09-27','targets':targets,'demo_mode':True},status=201)
    def submit(proposal):
        call(f"/ad-proposals/{proposal['id']}/confirm",{'expected_version':proposal['version']});call(f"/ad-proposals/{proposal['id']}/submit",{'expected_version':proposal['version']})
    submit(proposal)
    denied=client.post(f"/api/ad-proposals/{proposal['id']}/approve",headers=operator,json={'expected_version':proposal['version']})
    assert denied.status_code==403
    call(f"/ad-proposals/{proposal['id']}/reject",{'expected_version':proposal['version'],'comment':'请补充面料说明再提交'},role='manager')
    proposal=call(f"/ad-proposals/{proposal['id']}",{'expected_version':proposal['version'],'strategy':{**proposal['strategy'],'description':'补充面料特写说明，课堂人工执行'}},method='PATCH',status=201)
    submit(proposal)
    approved=call(f"/ad-proposals/{proposal['id']}/approve",{'expected_version':proposal['version'],'comment':'课堂方案批准'},role='manager',status=201)
    experiment=approved['experiment'];call(f"/experiments/{experiment['id']}/execution-note",{'execution_note':'历史示例数据；课堂人工模拟；未连接外部广告账户'},method='PATCH')
    def metric(day,group):
        return {'date':f'2026-09-{day:02d}','group_code':group,'impressions':8000 if group=='A' else 12000,'clicks':160 if group=='A' else 340,'orders':3 if group=='A' else 6,'gmv':'897.00' if group=='A' else '1794.00','ad_spend':'300.00','detail_visits':144 if group=='A' else 306,'detail_bounces':100 if group=='A' else 242}
    for group in ['A','B']:call(f"/experiments/{experiment['id']}/metrics",metric(21,group),status=201)
    rows=[metric(day,group) for day in range(22,28) for group in ['A','B']]
    rows.extend([{**metric(22,'A'),'ad_spend':'-1'},metric(21,'A')])
    fields=['date','shop_code','product_code','experiment_code','group_code','impressions','clicks','orders','gmv','ad_spend','detail_visits','detail_bounces']
    stream=io.StringIO();writer=csv.DictWriter(stream,fields);writer.writeheader()
    for item in rows:writer.writerow({**item,'shop_code':'SHOP-A','product_code':'SUN-001','experiment_code':f"EXP-{experiment['id']}"})
    directory=storage_root()/'demo';directory.mkdir(parents=True,exist_ok=True)
    from app.imports import template,FIELDS
    for kind in FIELDS:
        for format in ['csv','xlsx']:
            content,_=template(kind,format)
            (directory/f'{kind}-template.{format}').write_bytes(content)
    (directory/'metrics-with-errors-and-duplicates.csv').write_text('\ufeff'+stream.getvalue(),encoding='utf-8')
    response=client.post('/api/imports/preview',headers=operator,data={'import_type':'metrics'},files={'file':('metrics.csv',stream.getvalue().encode('utf-8'))})
    assert response.status_code==201,response.text
    batch=response.json();resolutions={str(x['row_number']):'skip' for x in batch['rows']['items'] if x['validation_status']=='duplicate'}
    imported=call(f"/imports/{batch['id']}/commit",{'row_resolutions':resolutions})
    assert imported['summary']['created']==12 and imported['summary']['failed']==1 and imported['summary']['skipped']==1
    summary=call(f"/experiments/{experiment['id']}/metrics",method='GET')
    assert summary['metrics']['ctr']==.025 and summary['metrics']['detail_bounce_rate']==.76
    report=call(f"/experiments/{experiment['id']}/close-period",{'demo_mode':True},status=201)
    call(f"/reviews/{report['id']}/confirm",{'expected_version':report['version']})
    second=call(f"/reviews/{report['id']}/next-round",{'expected_version':report['version'],'action_ids':['fabric-detail'],'demo_mode':True},status=201)
    result={'status':'passed','scope':'classroom example text; real API/database/worker/media/import/approval flow','product_id':product['id'],'shop_ids':[x['id'] for x in shops],'first_round_id':first['id'],'task_states':[x['status'] for x in states]+['cancelled'],'material_ids':[x['id'] for x in materials],'experiment_id':experiment['id'],'import_summary':imported['summary'],'metrics':summary['metrics'],'totals':summary['totals'],'review_id':report['id'],'second_round_id':second['round']['id']}
    (directory/'scenario-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

def main():
    with TestClient(app) as client:print(json.dumps(create_demo(client),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
