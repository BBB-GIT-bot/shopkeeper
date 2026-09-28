"""Mapped spreadsheet preview and independently atomic row imports."""
import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook
from .db import connect, transaction, json_value, parse_json, money_value

FIELDS = {
    'products':['product_code','name','category','base_price','attributes'],
    'inventory':['shop_code','product_code','sku_code','stock_quantity','warning_threshold','reason'],
    'competitors':['product_code','competitor_code','name','price','platform','attributes','selling_points','source_url','source_text'],
    'metrics':['date','shop_code','product_code','experiment_code','group_code','impressions','clicks','orders','gmv','ad_spend','detail_visits','detail_bounces'],
}
REQUIRED = {'products':FIELDS['products'][:4], 'inventory':FIELDS['inventory'][:4], 'competitors':FIELDS['competitors'][:4], 'metrics':FIELDS['metrics'][:10]}

def error(message, status=422):
    raise HTTPException(status, {'code':'import_validation','message':message})

def template(kind, format):
    if kind not in FIELDS: error('模板类型无效')
    stream=io.StringIO(); csv.writer(stream).writerow(FIELDS[kind])
    if format=='csv': return ('\ufeff'+stream.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8'
    if format!='xlsx': error('只支持 CSV 和 XLSX')
    workbook=Workbook(); sheet=workbook.active; sheet.title=kind; sheet.append(FIELDS[kind]); sheet.freeze_panes='A2'
    for col in sheet.columns: sheet.column_dimensions[col[0].column_letter].width=23
    out=io.BytesIO(); workbook.save(out)
    return out.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def parse_file(filename, raw, sheet_name=None):
    if len(raw)>5*1024*1024: error('文件不能超过 5 MB')
    suffix=Path(filename or '').suffix.lower()
    try:
        if suffix=='.csv':
            values=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
            sheets=[]
        elif suffix=='.xlsx':
            book=load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
            sheets=book.sheetnames
            if sheet_name and sheet_name not in sheets: error('工作表不存在')
            selected=book[sheet_name] if sheet_name else book.active
            values=list(selected.iter_rows(values_only=True)); sheet_name=selected.title
            book.close()
        else: error('请选择 UTF-8 CSV 或 XLSX 文件')
    except HTTPException: raise
    except Exception: error('无法解析文件，请检查格式及 UTF-8 编码')
    if not values or not values[0]: error('文件缺少表头')
    if len(values)>5001: error('单批最多 5000 行')
    headers=[str(x or '').strip() for x in values[0]]
    if any(not x for x in headers) or len(set(headers))!=len(headers): error('表头不能为空或重复')
    return headers, values[1:], sheets, sheet_name

def natural_key(kind, data):
    keys={'products':['product_code'],'inventory':['shop_code','product_code','sku_code'],'competitors':['product_code','competitor_code'],'metrics':['date','shop_code','product_code','experiment_code','group_code']}[kind]
    return json_value([data.get(key) for key in keys])

def nonnegative(value, name):
    if isinstance(value,bool) or not re.fullmatch(r'\d+',str(value)): raise ValueError(f'{name} 必须为非负整数')
    return int(value)

def validate(kind, raw, c):
    data={key:(str(value).strip() if not isinstance(value,(dict,list)) else value) for key,value in raw.items() if value is not None and value!=''}
    for key in REQUIRED[kind]:
        if data.get(key) in (None,''): raise ValueError(f'{key} 必填')
    for key in ('base_price','price','gmv','ad_spend'):
        if key in data:
            amount=money_value(data[key],key)
            if amount<0 or (key in {'base_price','price'} and amount<=0): raise ValueError(f'{key} 金额无效')
            data[key]=format(amount,'.2f')
    for key in ('stock_quantity','warning_threshold','impressions','clicks','orders','detail_visits','detail_bounces'):
        if key in data: data[key]=nonnegative(data[key],key)
    for key in ('attributes','selling_points'):
        if key in data and isinstance(data[key],str): data[key]=json.loads(data[key])
        if key in data and not isinstance(data[key],dict if key=='attributes' else list): raise ValueError(f'{key} JSON 类型无效')
    if kind=='products':
        resource=c.execute('SELECT id FROM products WHERE product_code=?',(data['product_code'],)).fetchone()
        return data, resource[0] if resource else None
    product=c.execute('SELECT id FROM products WHERE product_code=?',(data['product_code'],)).fetchone()
    if not product: raise ValueError('product_code 商品不存在')
    data['_product_id']=product[0]
    if kind=='competitors':
        resource=c.execute('SELECT id FROM competitors WHERE product_id=? AND competitor_code=?',(product[0],data['competitor_code'])).fetchone()
        return data, resource[0] if resource else None
    shop=c.execute('SELECT id FROM shops WHERE shop_code=?',(data['shop_code'],)).fetchone()
    if not shop: raise ValueError('shop_code 店铺不存在')
    data['_shop_id']=shop[0]
    if kind=='inventory':
        mapping=c.execute('SELECT m.id FROM sku_mappings m JOIN listings l ON l.id=m.listing_id JOIN skus s ON s.id=m.sku_id WHERE l.shop_id=? AND l.product_id=? AND s.sku_code=? AND s.product_id=?',(shop[0],product[0],data['sku_code'],product[0])).fetchone()
        if not mapping: raise ValueError('店铺商品与 SKU 映射不存在')
        return data, mapping[0]
    try: data['date']=date.fromisoformat(data['date'][:10]).isoformat()
    except ValueError: raise ValueError('date 必须为 YYYY-MM-DD')
    code=data['experiment_code']
    match=re.fullmatch(r'(?:EXP-)?(\d+)',code)
    exp=c.execute('SELECT * FROM experiments WHERE id=?',(int(match.group(1)),)).fetchone() if match else None
    if not exp: raise ValueError('experiment_code 实验不存在，使用 EXP-实验编号')
    if exp['status']!='approved' or exp['shop_id']!=shop[0] or exp['product_id']!=product[0]: raise ValueError('实验未批准或不属于该商品／店铺')
    if not exp['date_from']<=data['date']<=exp['date_to']: raise ValueError('日期不在实验周期')
    proposal=c.execute('SELECT groups_data FROM proposals WHERE id=?',(exp['proposal_id'],)).fetchone()
    if data['group_code'] not in {g['group_code'] for g in parse_json(proposal[0],[])}: raise ValueError('group_code 不属于实验')
    if ('detail_visits' in data)!=('detail_bounces' in data): raise ValueError('详情页访问与跳失必须成对填写')
    if data.get('detail_bounces',0)>data.get('detail_visits',0): raise ValueError('跳失不能超过访问')
    data['_experiment_id']=exp['id']
    resource=c.execute('SELECT id FROM metrics WHERE experiment_id=? AND date=? AND group_code=?',(exp['id'],data['date'],data['group_code'])).fetchone()
    return data,resource[0] if resource else None

def preview(kind, filename, raw, mapping, sheet_name):
    if kind not in FIELDS: error('导入类型无效')
    headers,values,sheets,selected=parse_file(filename,raw,sheet_name)
    if not isinstance(mapping,dict) or any(key not in headers or val not in FIELDS[kind] for key,val in mapping.items()): error('字段映射无效，键为原表头、值为标准字段')
    mapped=[mapping.get(key,key) for key in headers]
    if len(set(mapped))!=len(mapped): error('多个表头不能映射到同一字段')
    if any(key not in mapped for key in REQUIRED[kind]): error('缺少必填表头，请完成字段映射')
    rows=[]; groups={}
    with connect() as c:
        for number,values in enumerate(values,2):
            if not any(value is not None and str(value).strip() for value in values): continue
            raw={mapped[i]:value for i,value in enumerate(values) if i<len(mapped) and mapped[i] in FIELDS[kind]}
            item={'row_number':number,'normalized_data':raw,'details':[],'validation_status':'valid','existing_resource_id':None,'duplicate_group':None,'commit_status':None}
            try:
                if any(isinstance(v,str) and v.startswith('=') for v in raw.values()): raise ValueError('不接受公式，请粘贴实际值')
                data,existing=validate(kind,raw,c)
                item['normalized_data']={k:v for k,v in data.items() if not k.startswith('_')}; item['existing_resource_id']=existing
                key=natural_key(kind,data); groups.setdefault(key,[]).append(item)
                if existing and kind!='inventory': item['validation_status']='duplicate'; item['duplicate_group']=key
            except Exception as exc:
                item['validation_status']='invalid'; item['details']=[{'field':'row','message':str(exc) if isinstance(exc,(ValueError,TypeError)) else '该行数据校验失败'}]
            rows.append(item)
    for key,items in groups.items():
        if len(items)>1:
            for item in items: item.update(validation_status='duplicate',duplicate_group=key)
    summary={'total':len(rows), **{key:sum(x['validation_status']==key for x in rows) for key in ('valid','invalid','duplicate')},'created':0,'updated':0,'skipped':0,'failed':0}
    with transaction() as c:
        batch=c.execute('INSERT INTO imports(import_type,filename,rows_data,summary,field_mapping,sheet_name) VALUES (?,?,?,?,?,?)',(kind,filename,json_value(rows),json_value(summary),json_value(mapping),selected)).lastrowid
    return batch,headers,sheets

def write_row(kind, data, existing, user_id, c):
    if kind=='products':
        values=(data['name'],data['category'],data['base_price'],json_value(data.get('attributes',{})))
        if existing: c.execute('UPDATE products SET name=?,category=?,base_price=?,attributes=? WHERE id=?',(*values,existing))
        else: c.execute('INSERT INTO products(product_code,name,category,base_price,attributes) VALUES (?,?,?,?,?)',(data['product_code'],*values))
    elif kind=='competitors':
        values=(data['name'],data['price'],data.get('platform',''),json_value(data.get('attributes',{})),json_value(data.get('selling_points',[])),data.get('source_url',''),data.get('source_text',''))
        if existing: c.execute("UPDATE competitors SET name=?,price=?,platform=?,attributes=?,selling_points=?,source_url=?,source_text=?,verification_status='unverified' WHERE id=?",(*values,existing))
        else: c.execute('INSERT INTO competitors(product_id,competitor_code,name,price,platform,attributes,selling_points,source_url,source_text) VALUES (?,?,?,?,?,?,?,?,?)',(data['_product_id'],data['competitor_code'],*values))
    elif kind=='inventory':
        old=c.execute('SELECT stock_quantity,warning_threshold FROM sku_mappings WHERE id=?',(existing,)).fetchone()
        target=data['stock_quantity']
        c.execute('UPDATE sku_mappings SET stock_quantity=?,warning_threshold=? WHERE id=?',(target,data.get('warning_threshold',old['warning_threshold']),existing))
        c.execute('INSERT INTO stock_movements(mapping_id,before_quantity,after_quantity,delta,reason,source,actor_id) VALUES (?,?,?,?,?,?,?)',(existing,old['stock_quantity'],target,target-old['stock_quantity'],data.get('reason','表格导入目标库存'),'import',user_id))
    else:
        values=tuple(data.get(k) for k in ('impressions','clicks','orders','gmv','ad_spend','detail_visits','detail_bounces'))
        if existing: c.execute('UPDATE metrics SET impressions=?,clicks=?,orders=?,gmv=?,ad_spend=?,detail_visits=?,detail_bounces=? WHERE id=?',(*values,existing))
        else: c.execute('INSERT INTO metrics(experiment_id,date,group_code,impressions,clicks,orders,gmv,ad_spend,detail_visits,detail_bounces) VALUES (?,?,?,?,?,?,?,?,?,?)',(data['_experiment_id'],data['date'],data['group_code'],*values))

def commit(batch_id, resolutions, user_id):
    if isinstance(resolutions,list):
        if len({x.get('row_number') for x in resolutions})!=len(resolutions): error('行处理选择重复')
        resolutions={str(x.get('row_number')):x.get('action') for x in resolutions}
    if not isinstance(resolutions,dict) or any(v not in {'keep','skip','update'} for v in resolutions.values()): error('重复行处理选项无效')
    with transaction() as c:
        batch=c.execute('SELECT * FROM imports WHERE id=?',(batch_id,)).fetchone()
        if not batch: error('批次不存在',404)
        if batch['status']!='previewed': error('批次已经提交或正在提交，请查询结果',409)
        rows=parse_json(batch['rows_data'],[]); summary=parse_json(batch['summary'],{}); groups={}
        for row in rows:
            if row['validation_status']=='duplicate': groups.setdefault(row['duplicate_group'],[]).append(row)
        for items in groups.values():
            decisions=[resolutions.get(str(row['row_number'])) for row in items]
            if any(x is None for x in decisions) or sum(x!='skip' for x in decisions)>1: error('重复组须明确保留一行，其余跳过，也可全部跳过')
            for row,action in zip(items,decisions):
                if row['existing_resource_id'] and action=='keep' and batch['import_type']!='inventory': error('数据库已有记录，请选更新或跳过')
        claimed=c.execute("UPDATE imports SET status='committing' WHERE id=? AND status='previewed'",(batch_id,))
        if claimed.rowcount!=1: error('批次已被其他请求提交',409)
    for item in rows:
        action=resolutions.get(str(item['row_number']))
        if action=='skip': status='skipped'
        elif item['validation_status']=='invalid': status='failed'
        else:
            try:
                with transaction() as c:
                    data,existing=validate(batch['import_type'],item['normalized_data'],c)
                    if existing and batch['import_type']!='inventory' and action!='update': raise ValueError('预览后出现重复记录，请在新批次确认更新')
                    if action=='update' and not existing: raise ValueError('待更新记录已不存在')
                    write_row(batch['import_type'],data,existing,user_id,c)
                    status='updated' if existing else 'created'
                    item['commit_status']=status; next_summary={**summary,status:summary[status]+1}
                    c.execute('UPDATE imports SET rows_data=?,summary=? WHERE id=?',(json_value(rows),json_value(next_summary),batch_id))
                summary=next_summary
                continue
            except Exception as exc:
                status='failed'; item['details'].append({'field':'row','message':str(exc) if isinstance(exc,ValueError) else '写入失败，该行已完整回滚'})
        item['commit_status']=status; summary[status]+=1
        with transaction() as c: c.execute('UPDATE imports SET rows_data=?,summary=? WHERE id=?',(json_value(rows),json_value(summary),batch_id))
    with transaction() as c: c.execute("UPDATE imports SET status='committed' WHERE id=?",(batch_id,))
    return {'id':batch_id,'status':'committed','summary':summary}
