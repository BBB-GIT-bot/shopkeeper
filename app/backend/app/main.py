from __future__ import annotations

import csv
import base64
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationError

from .auth import EXPIRES, create_token, require_role, verify_password, current_user
from .db import DatabaseValueError, connect, init_db, is_duplicate, json_value, money_value, parse_json, row, transaction
from decimal import Decimal
from .config import storage_root
from .scripts.seed import seed_accounts
from .settings import AuthorizationPatch, QueuePatch, ServicePatch, decrypt_key, encrypt_key, public_queue, public_services
from . import tasks as task_service
from . import imports as import_service
from . import workflows as workflow

app = FastAPI(title="电商运营助手 API", version="1.0.0")

@app.exception_handler(sa.exc.IntegrityError)
async def constraint_error(request,exc):
    duplicate=is_duplicate(exc)
    return JSONResponse(status_code=409 if duplicate else 422,content={'code':'duplicate_record' if duplicate else 'invalid_reference','message':'记录已存在或状态已被其他请求更新' if duplicate else '关联记录不存在或数据违反约束'})


@app.exception_handler(DatabaseValueError)
async def database_value_error(request, exc):
    return JSONResponse(status_code=422, content={'code': 'validation_error', 'message': exc.reason, 'details': [{'field': exc.field, 'message': exc.reason}]})


@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def validation_error(request, exc):
    # Pydantic's input values may include credentials; never echo them.
    details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"code": "validation_error", "message": "请求字段校验失败", "details": details})


app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
ROOT = Path(__file__).resolve().parents[1]
STORAGE = storage_root()
try:
    STORAGE.mkdir(parents=True, exist_ok=True)
except PermissionError:
    STORAGE = Path(os.getenv("TEMP", ".")) / "shopkeeper-storage"
    STORAGE.mkdir(parents=True, exist_ok=True)


class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")


class LoginPayload(BaseModel):
    username: str
    password: str


class StockPayload(BaseModel):
    stock_quantity: int = Field(ge=0,strict=True)
    reason: str = Field(min_length=1)


class ReviewPayload(BaseModel):
    decision: Literal["usable", "rejected"]
    comment: str = ""


class MetricPayload(BaseModel):
    date: date
    group_code: str
    impressions: int = Field(ge=0,strict=True)
    clicks: int = Field(ge=0,strict=True)
    orders: int = Field(ge=0,strict=True)
    gmv: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    ad_spend: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    detail_visits: int | None = Field(default=None, ge=0,strict=True)
    detail_bounces: int | None = Field(default=None, ge=0,strict=True)


class ImportCommit(BaseModel):
    row_resolutions: dict[str, str] | list[dict[str, Any]] = Field(default_factory=dict)


def fail(status: int, code: str, message: str, details: Any = None):
    detail = {"code": code, "message": message}
    if details is not None:
        detail["details"] = details
    raise HTTPException(status_code=status, detail=detail)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def demo_png() -> bytes:
    # A valid 1x1 PNG keeps the demo material previewable without external tools.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


def output(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        fail(404, "not_found", "资源不存在")
    for key in list(value):
        if key.endswith("_data") or key in {"attributes", "selling_points", "content", "edited_content", "strategy", "targets", "material_ids", "groups", "groups_data", "sections", "actions", "snapshot", "config_snapshot", "tags", "review_history", "selected_actions"}:
            value[key.removesuffix("_data")] = parse_json(value.pop(key), value.get(key))
    for key in ("on_sale", "enabled", "is_demo"):
        if key in value:
            value[key] = bool(value[key])
    for key in ('base_price', 'sale_price', 'price', 'gmv', 'ad_spend'):
        if key in value and value[key] is not None:
            value[key] = format(Decimal(str(value[key])), '.2f')
    return value


def page(rows: list[dict[str, Any]], total: int, page_no: int, page_size: int) -> dict[str, Any]:
    return {"items": rows, "total": total, "page": page_no, "page_size": page_size}


def parse_page(page_no: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> tuple[int, int]:
    return page_no, page_size


def latest_content(connection, round_id: int, kind: str):
    return connection.execute("SELECT * FROM contents WHERE round_id=? AND kind=? ORDER BY version DESC LIMIT 1", (round_id, kind)).fetchone()


def validate_content(kind: str, content: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(content,dict): return ['内容须为结构化对象']
    if kind == "diagnosis":
        for field in ("target_audience", "price_band_analysis", "selling_point_matrix", "conversion_bottlenecks", "optimization_directions"):
            if not content.get(field):
                errors.append(field)
    if kind == "creative":
        images = content.get("image_directions", [])
        videos = content.get("video_scripts", [])
        if not isinstance(images,list) or not isinstance(videos,list): return ['主图和视频方案须为数组']
        if any(not isinstance(item,dict) for item in images+videos): return ['每项创意须为结构化对象']
        if len(images) < 3:
            errors.append("image_directions 至少 3 组")
        if len(videos) < 3:
            errors.append("video_scripts 至少 3 套")
        for item in images:
            for field in ("selection_id", "title", "layout", "core_copy", "main_selling_point", "visual_advice"):
                if not item.get(field):
                    errors.append(f"image:{field}")
        for item in videos:
            for field in ("selection_id", "title", "hook_3s", "shots", "cta"):
                if not item.get(field):
                    errors.append(f"video:{field}")
            if not isinstance(item.get("shots"), list) or not item["shots"]:
                errors.append("video:shots")
            elif any(not isinstance(shot,dict) or not shot.get('scene_description') or not shot.get('voiceover') for shot in item['shots']): errors.append('每个分镜须包含画面描述与口播台词')
        for pool,key in [(images,'selected_image_ids'),(videos,'selected_video_ids')]:
            ids=[item.get('selection_id') for item in pool]
            if len(set(ids))!=len(ids): errors.append('方案编码不能重复')
            selected=content.get(key,[])
            if not isinstance(selected,list) or any(x not in ids for x in selected): errors.append(f'{key} 选定方案不存在')
    return errors


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_accounts()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "shopkeeper-api"}


@app.post("/api/auth/login")
def login(payload: LoginPayload):
    with connect() as connection:
        user = connection.execute("SELECT * FROM users WHERE username=?", (payload.username,)).fetchone()
    if not user or not user["enabled"] or not verify_password(payload.password, user["password_hash"]):
        fail(401, "invalid_credentials", "用户名或密码错误")
    user_data = {"id": user["id"], "username": user["username"], "role": user["role"], "enabled": bool(user["enabled"])}
    return {"access_token": create_token(user_data), "token_type": "bearer", "expires_in": EXPIRES, "user": user_data}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/settings/services")
def get_services(user=Depends(require_role("manager"))):
    with connect() as connection:
        item = row(connection.execute("SELECT * FROM service_configs WHERE id=1").fetchone())
    return public_services(item)


@app.patch("/api/settings/services")
def patch_services(payload: ServicePatch, user=Depends(require_role("manager"))):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        fail(422, "empty_patch", "没有可修改字段")
    with transaction() as connection:
        current = row(connection.execute("SELECT * FROM service_configs WHERE id=1").fetchone())
        key = data.pop("api_key", None)
        if key is not None or current.get("api_key"):
            data["api_key_ciphertext"] = encrypt_key(key or current["api_key"])
            data["api_key"] = ""
        data["updated_by"] = user["id"]
        data["updated_at"] = now()
        sets = ",".join(f"{key}=?" for key in data)
        connection.execute(f"UPDATE service_configs SET {sets} WHERE id=1", tuple(data.values()))
        item = row(connection.execute("SELECT * FROM service_configs WHERE id=1").fetchone())
    return public_services(item)


@app.get("/api/settings/queue")
def get_queue(user=Depends(require_role("manager"))):
    with connect() as connection:
        return public_queue(row(connection.execute("SELECT * FROM queue_configs WHERE id=1").fetchone()))


@app.patch("/api/settings/queue")
def patch_queue(payload: QueuePatch, user=Depends(require_role("manager"))):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        fail(422, "empty_patch", "没有可修改字段")
    with transaction() as connection:
        current = row(connection.execute("SELECT * FROM queue_configs WHERE id=1").fetchone())
        merged = {**current, **data}
        if merged["heartbeat_interval_seconds"] >= merged["execution_timeout_seconds"]:
            fail(422, "invalid_queue_config", "心跳间隔必须小于单次执行超时")
        data.update(updated_by=user["id"], updated_at=now())
        sets = ",".join(f"{key}=?" for key in data)
        connection.execute(f"UPDATE queue_configs SET {sets} WHERE id=1", tuple(data.values()))
        return public_queue(row(connection.execute("SELECT * FROM queue_configs WHERE id=1").fetchone()))


@app.get("/api/shops")
def list_shops(page_no: int = Query(1, alias="page", ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(current_user)):
    with connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        rows = [output(row(item)) for item in connection.execute("SELECT * FROM shops ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (page_no - 1) * page_size))]
    return page(rows, total, page_no, page_size)


@app.post("/api/shops", status_code=201)
def create_shop(payload: Payload, user=Depends(require_role("operator"))):
    required = ("shop_code", "platform", "name", "platform_shop_id")
    if any(not payload.model_dump().get(key) for key in required):
        fail(422, "missing_field", "店铺字段不完整")
    try:
        with transaction() as connection:
            cursor = connection.execute("INSERT INTO shops(shop_code,platform,name,platform_shop_id) VALUES (?,?,?,?)", tuple(payload.model_dump()[key] for key in required))
            return output(row(connection.execute("SELECT * FROM shops WHERE id=?", (cursor.lastrowid,)).fetchone()))
    except Exception as exc:
        if is_duplicate(exc):
            fail(409, "duplicate_shop", "店铺编码已存在")
        raise


@app.patch("/api/shops/{shop_id}")
def patch_shop(shop_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = {key: value for key, value in payload.model_dump().items() if key in {"name", "platform", "platform_shop_id"}}
    if not data:
        fail(422, "empty_patch", "没有可修改字段")
    with transaction() as connection:
        if not connection.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
            fail(404, "not_found", "店铺不存在")
        sets = ",".join(f"{key}=?" for key in data)
        connection.execute(f"UPDATE shops SET {sets} WHERE id=?", (*data.values(), shop_id))
        return output(row(connection.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()))


@app.patch("/api/settings/shops/{shop_id}/authorization")
def patch_authorization(shop_id: int, payload: AuthorizationPatch, user=Depends(require_role("manager"))):
    status_value = payload.model_dump().get("authorization_status")
    if status_value not in {"not_configured", "authorized", "expired"}:
        fail(422, "invalid_authorization", "授权状态无效")
    with transaction() as connection:
        connection.execute("UPDATE shops SET authorization_status=? WHERE id=?", (status_value, shop_id))
        item = connection.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not item:
            fail(404, "not_found", "店铺不存在")
        return output(row(item))


@app.get("/api/products")
def list_products(keyword: str | None = None, shop_id: int | None = None, page_no: int = Query(1, alias="page", ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(current_user)):
    where, args = [], []
    if keyword:
        where.append("(p.name LIKE ? OR p.product_code LIKE ?)"); args.extend([f"%{keyword}%", f"%{keyword}%"])
    if shop_id:
        where.append("EXISTS (SELECT 1 FROM listings l WHERE l.product_id=p.id AND l.shop_id=?)"); args.append(shop_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM products p {clause}", args).fetchone()[0]
        rows = [output(row(item)) for item in connection.execute(f"SELECT p.* FROM products p {clause} ORDER BY p.id DESC LIMIT ? OFFSET ?", (*args, page_size, (page_no - 1) * page_size))]
    return page(rows, total, page_no, page_size)


@app.post("/api/products", status_code=201)
def create_product(payload: Payload, user=Depends(require_role("operator"))):
    data = payload.model_dump()
    required = ("product_code", "name", "category", "base_price")
    if any(data.get(key) in (None, "") for key in required) or money_value(data["base_price"], 'base_price') <= 0:
        fail(422, "invalid_product", "商品字段不完整或售价必须为正数")
    try:
        with transaction() as connection:
            cursor = connection.execute("INSERT INTO products(product_code,name,category,base_price,attributes) VALUES (?,?,?,?,?)", (data["product_code"], data["name"], data["category"], data["base_price"], json_value(data.get("attributes", {}))))
            return output(row(connection.execute("SELECT * FROM products WHERE id=?", (cursor.lastrowid,)).fetchone()))
    except Exception as exc:
        if is_duplicate(exc): fail(409, "duplicate_product", "商品编码已存在")
        raise


@app.get("/api/products/{product_id}")
def get_product(product_id: int, user=Depends(current_user)):
    with connect() as connection:
        product = output(row(connection.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()))
        if not product: fail(404, "not_found", "商品不存在")
        product["skus"] = [output(row(x)) for x in connection.execute("SELECT * FROM skus WHERE product_id=? ORDER BY id", (product_id,))]
        product["listings"] = []
        for listing in connection.execute("SELECT * FROM listings WHERE product_id=? ORDER BY id", (product_id,)):
            item = output(row(listing)); item["sku_mappings"] = [output(row(x)) for x in connection.execute("SELECT * FROM sku_mappings WHERE listing_id=? ORDER BY id", (listing["id"],))]; product["listings"].append(item)
        product["rounds"] = [output(row(x)) for x in connection.execute("SELECT * FROM rounds WHERE product_id=? ORDER BY round_no", (product_id,))]
        return product


@app.patch("/api/products/{product_id}")
def patch_product(product_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = {key: value for key, value in payload.model_dump().items() if key in {"name", "category", "base_price", "attributes", "main_sku_id"}}
    if not data: fail(422, "empty_patch", "没有可修改字段")
    if "base_price" in data and money_value(data["base_price"], 'base_price') <= 0: fail(422, "invalid_price", "售价必须为正数")
    with transaction() as connection:
        if not connection.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone(): fail(404, "not_found", "商品不存在")
        if data.get('main_sku_id') is not None and not connection.execute('SELECT 1 FROM skus WHERE id=? AND product_id=?',(data['main_sku_id'],product_id)).fetchone(): fail(422,'ownership_mismatch','主推规格不属于当前商品')
        if "attributes" in data: data["attributes"] = json_value(data["attributes"])
        sets = ",".join(f"{key}=?" for key in data); connection.execute(f"UPDATE products SET {sets} WHERE id=?", (*data.values(), product_id))
        return output(row(connection.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()))


@app.get("/api/products/{product_id}/skus")
def list_skus(product_id: int, user=Depends(current_user)):
    with connect() as connection:
        rows = [output(row(x)) for x in connection.execute("SELECT * FROM skus WHERE product_id=? ORDER BY id", (product_id,))]
    return page(rows, len(rows), 1, len(rows) or 50)


@app.post("/api/products/{product_id}/skus", status_code=201)
def create_sku(product_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = payload.model_dump()
    if not data.get("sku_code"): fail(422, "missing_field", "sku_code 必填")
    try:
        with transaction() as connection:
            if not connection.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone(): fail(404, "not_found", "商品不存在")
            cursor = connection.execute("INSERT INTO skus(product_id,sku_code,attributes) VALUES (?,?,?)", (product_id, data["sku_code"], json_value(data.get("attributes", {}))))
            return output(row(connection.execute("SELECT * FROM skus WHERE id=?", (cursor.lastrowid,)).fetchone()))
    except Exception as exc:
        if is_duplicate(exc): fail(409, "duplicate_sku", "商品内 SKU 编码已存在")
        raise


@app.patch("/api/skus/{sku_id}")
def patch_sku(sku_id: int, payload: Payload, user=Depends(require_role("operator"))):
    attrs = payload.model_dump().get("attributes")
    if attrs is None: fail(422, "empty_patch", "只允许修改 attributes")
    with transaction() as connection:
        connection.execute("UPDATE skus SET attributes=? WHERE id=?", (json_value(attrs), sku_id)); item = connection.execute("SELECT * FROM skus WHERE id=?", (sku_id,)).fetchone()
        return output(row(item))


@app.get("/api/products/{product_id}/listings")
def list_listings(product_id: int, user=Depends(current_user)):
    with connect() as connection:
        listings = []
        for listing in connection.execute("SELECT * FROM listings WHERE product_id=?", (product_id,)):
            item = output(row(listing)); item["sku_mappings"] = [output(row(x)) for x in connection.execute("SELECT * FROM sku_mappings WHERE listing_id=?", (listing["id"],))]; listings.append(item)
    return page(listings, len(listings), 1, len(listings) or 50)


@app.post("/api/products/{product_id}/listings", status_code=201)
def create_listing(product_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = payload.model_dump()
    try:
        with transaction() as connection:
            if not connection.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone(): fail(404, "not_found", "商品不存在")
            cursor = connection.execute("INSERT INTO listings(product_id,shop_id,platform_product_id,on_sale) VALUES (?,?,?,?)", (product_id, data["shop_id"], data["platform_product_id"], int(data.get("on_sale", True))))
            return output(row(connection.execute("SELECT * FROM listings WHERE id=?", (cursor.lastrowid,)).fetchone()))
    except Exception as exc:
        if is_duplicate(exc): fail(409, "duplicate_listing", "商品已在该店铺上架")
        raise


@app.patch("/api/listings/{listing_id}")
def patch_listing(listing_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = {key: value for key, value in payload.model_dump().items() if key in {"platform_product_id", "on_sale"}}
    if not data: fail(422, "empty_patch", "没有可修改字段")
    sets = ",".join(f"{key}=?" for key in data)
    with transaction() as connection:
        connection.execute(f"UPDATE listings SET {sets} WHERE id=?", (*data.values(), listing_id)); return output(row(connection.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()))


@app.get("/api/listings/{listing_id}/sku-mappings")
def list_mappings(listing_id: int, user=Depends(current_user)):
    with connect() as connection: rows = [output(row(x)) for x in connection.execute("SELECT * FROM sku_mappings WHERE listing_id=?", (listing_id,))]
    return page(rows, len(rows), 1, len(rows) or 50)


@app.post("/api/listings/{listing_id}/sku-mappings", status_code=201)
def create_mapping(listing_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = payload.model_dump(); required = ("sku_id", "platform_sku_id", "sale_price", "stock_quantity", "warning_threshold")
    if any(not isinstance(data.get(key),int) or isinstance(data.get(key),bool) for key in ('stock_quantity','warning_threshold')): fail(422,'invalid_mapping','库存和阈值须为非负整数')
    if any(key not in data for key in required) or data["stock_quantity"] < 0 or data["warning_threshold"] < 0 or money_value(data["sale_price"], 'sale_price') <= 0: fail(422, "invalid_mapping", "映射字段无效")
    with transaction() as connection:
        listing = connection.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone(); sku = connection.execute("SELECT * FROM skus WHERE id=?", (data["sku_id"],)).fetchone()
        if not listing or not sku or listing["product_id"] != sku["product_id"]: fail(422, "ownership_mismatch", "SKU 不属于当前商品/上架记录")
        try:
            cursor = connection.execute("INSERT INTO sku_mappings(listing_id,sku_id,platform_sku_id,sale_price,stock_quantity,warning_threshold) VALUES (?,?,?,?,?,?)", (listing_id, data["sku_id"], data["platform_sku_id"], data["sale_price"], data["stock_quantity"], data["warning_threshold"]))
        except Exception as exc:
            if is_duplicate(exc): fail(409, "duplicate_mapping", "SKU 映射已存在")
            raise
        mapping_id = cursor.lastrowid
        connection.execute("INSERT INTO stock_movements(mapping_id,before_quantity,after_quantity,delta,reason,source,actor_id) VALUES (?,?,?,?,?,?,?)", (mapping_id, 0, data["stock_quantity"], data["stock_quantity"], "初始化库存", "manual", user["id"]))
        return output(row(connection.execute("SELECT * FROM sku_mappings WHERE id=?", (mapping_id,)).fetchone()))


@app.patch("/api/sku-mappings/{mapping_id}")
def patch_mapping(mapping_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = {key: value for key, value in payload.model_dump().items() if key in {"platform_sku_id", "sale_price", "warning_threshold"}}
    if "sale_price" in data and money_value(data["sale_price"], 'sale_price') <= 0: fail(422, "invalid_price", "售价必须为正数")
    if "warning_threshold" in data and data["warning_threshold"] < 0: fail(422, "invalid_threshold", "阈值不可为负")
    if not data: fail(422, "empty_patch", "库存必须通过库存调整接口修改")
    with transaction() as connection:
        sets = ",".join(f"{key}=?" for key in data); connection.execute(f"UPDATE sku_mappings SET {sets} WHERE id=?", (*data.values(), mapping_id)); return output(row(connection.execute("SELECT * FROM sku_mappings WHERE id=?", (mapping_id,)).fetchone()))


@app.post("/api/sku-mappings/{mapping_id}/stock-adjustments")
def adjust_stock(mapping_id: int, payload: StockPayload, user=Depends(require_role("operator"))):
    with transaction() as connection:
        mapping = connection.execute("SELECT * FROM sku_mappings WHERE id=?", (mapping_id,)).fetchone()
        if not mapping: fail(404, "not_found", "库存映射不存在")
        before, after = mapping["stock_quantity"], payload.stock_quantity
        connection.execute("UPDATE sku_mappings SET stock_quantity=? WHERE id=?", (after, mapping_id))
        cursor = connection.execute("INSERT INTO stock_movements(mapping_id,before_quantity,after_quantity,delta,reason,source,actor_id) VALUES (?,?,?,?,?,?,?)", (mapping_id, before, after, after - before, payload.reason, "manual", user["id"]))
        return {"sku_mapping": output(row(connection.execute("SELECT * FROM sku_mappings WHERE id=?", (mapping_id,)).fetchone())), "stock_movement": output(row(connection.execute("SELECT * FROM stock_movements WHERE id=?", (cursor.lastrowid,)).fetchone()))}


@app.get("/api/sku-mappings/{mapping_id}/stock-movements")
def stock_movements(mapping_id: int, user=Depends(current_user)):
    with connect() as connection: rows = [output(row(x)) for x in connection.execute("SELECT * FROM stock_movements WHERE mapping_id=? ORDER BY id DESC", (mapping_id,))]
    return page(rows, len(rows), 1, len(rows) or 50)


@app.get("/api/products/{product_id}/competitors")
def list_competitors(product_id: int, verification_status: str | None = None, user=Depends(current_user)):
    query = "SELECT * FROM competitors WHERE product_id=?"; args: list[Any] = [product_id]
    if verification_status: query += " AND verification_status=?"; args.append(verification_status)
    with connect() as connection: rows = [output(row(x)) for x in connection.execute(query, args)]
    return page(rows, len(rows), 1, len(rows) or 50)


@app.post("/api/products/{product_id}/competitors", status_code=201)
def create_competitor(product_id: int, payload: Payload, user=Depends(require_role("operator"))):
    data = payload.model_dump(); required = ("competitor_code", "name", "price")
    if any(data.get(k) in (None, "") for k in required) or money_value(data["price"], 'price') <= 0: fail(422, "invalid_competitor", "竞品字段不完整")
    try:
        with transaction() as connection:
            cursor = connection.execute("INSERT INTO competitors(product_id,competitor_code,name,price,platform,attributes,selling_points,source_url,source_text) VALUES (?,?,?,?,?,?,?,?,?)", (product_id,data["competitor_code"],data["name"],data["price"],data.get("platform",""),json_value(data.get("attributes",{})),json_value(data.get("selling_points",[])),data.get("source_url",""),data.get("source_text","")))
            return output(row(connection.execute("SELECT * FROM competitors WHERE id=?", (cursor.lastrowid,)).fetchone()))
    except Exception as exc:
        if is_duplicate(exc): fail(409, "duplicate_competitor", "竞品编码已存在")
        raise


@app.post("/api/products/{product_id}/competitors/parse")
def parse_competitor(product_id: int, payload: Payload, user=Depends(require_role("operator"))):
    text = payload.model_dump().get("source_text", "")
    if not text: fail(422, "missing_source_text", "公开商品文本不能为空")
    price = re.search(r"(?:¥|￥|价格?[:：]?\s*)(\d+(?:\.\d+)?)", text)
    return {"parsed_data": {"name": text[:40], "price": float(price.group(1)) if price else 0, "attributes": {}, "selling_points": [x.strip() for x in re.split("[，,。]", text) if x.strip()][:5], "source_text": text, "source_url": payload.model_dump().get("source_url", "")}, "verification_status": "unverified"}


@app.patch("/api/competitors/{competitor_id}")
def patch_competitor(competitor_id: int, payload: Payload, user=Depends(require_role("operator"))):
    allowed = {"competitor_code","name","price","platform","attributes","selling_points","source_url","source_text","verification_status"}; data = {k:v for k,v in payload.model_dump().items() if k in allowed}
    if "attributes" in data: data["attributes"] = json_value(data["attributes"])
    if "selling_points" in data: data["selling_points"] = json_value(data["selling_points"])
    if "verification_status" in data and data["verification_status"] not in {"unverified","verified"}: fail(422,"invalid_status","核对状态无效")
    if not data: fail(422,"empty_patch","没有可修改字段")
    with transaction() as connection:
        sets=",".join(f"{k}=?" for k in data); connection.execute(f"UPDATE competitors SET {sets} WHERE id=?",(*data.values(),competitor_id)); return output(row(connection.execute("SELECT * FROM competitors WHERE id=?",(competitor_id,)).fetchone()))


@app.get("/api/products/{product_id}/rounds")
def list_rounds(product_id:int,user=Depends(current_user)):
    with connect() as c:
        rounds=[]
        for item in c.execute('SELECT * FROM rounds WHERE product_id=? ORDER BY round_no DESC',(product_id,)).fetchall():
            data=output(dict(item))
            contents=[output(dict(x)) for x in c.execute('SELECT id,product_id,round_id,kind,version,status,archived_at FROM contents WHERE round_id=? ORDER BY version DESC',(item['id'],))]
            proposals=[proposal_output(dict(x)) for x in c.execute('SELECT * FROM proposals WHERE round_id=? ORDER BY version DESC',(item['id'],))]
            experiments=[output(dict(x)) for x in c.execute('SELECT e.* FROM experiments e JOIN proposals p ON p.id=e.proposal_id WHERE p.round_id=? ORDER BY e.id DESC',(item['id'],))]
            reviews=[output(dict(x)) for x in c.execute('SELECT * FROM reviews WHERE round_id=? ORDER BY version DESC',(item['id'],))]
            data['resource_index']={'contents':contents,'ad_proposals':proposals,'experiments':experiments,'reviews':reviews}
            data['phase']='review' if reviews else 'metrics' if experiments else 'approval' if proposals else 'materials' if any(x['kind']=='creative' and x['status']=='confirmed' for x in contents) else 'creative' if any(x['kind']=='diagnosis' and x['status']=='confirmed' for x in contents) else 'diagnosis'
            rounds.append(data)
    return page(rounds,len(rounds),1,len(rounds) or 50)


@app.post("/api/products/{product_id}/rounds", status_code=201)
def create_round(product_id: int, user=Depends(require_role("operator"))):
    with transaction() as connection:
        if not connection.execute("SELECT 1 FROM products WHERE id=?",(product_id,)).fetchone(): fail(404,"not_found","商品不存在")
        count=connection.execute("SELECT COUNT(*) FROM rounds WHERE product_id=?",(product_id,)).fetchone()[0]
        if count and not connection.execute("SELECT 1 FROM reviews WHERE product_id=? AND status='confirmed'",(product_id,)).fetchone(): fail(409,"round_exists","首轮已存在，后续轮次需从已确认复盘创建")
        cursor=connection.execute("INSERT INTO rounds(product_id,round_no) VALUES (?,?)",(product_id,count+1)); return output(row(connection.execute("SELECT * FROM rounds WHERE id=?",(cursor.lastrowid,)).fetchone()))


def generated_diagnosis() -> dict[str,Any]:
    return {"target_audience":["夏季通勤及户外防晒人群"],"price_band_analysis":"主流价格带 199-399 元，299 元具备竞争空间","selling_point_matrix":[{"selling_point":"凉感透气","evidence":"轻薄面料","recommendation":"主图突出透气结构"}],"conversion_bottlenecks":["详情页缺少面料细节说明"],"optimization_directions":["增加面料特写与尺码说明"]}


def generated_creative() -> dict[str,Any]:
    images=[{"selection_id":f"image-{i}","title":title,"layout":"左图右文" if i%2 else "主体居中","core_copy":copy,"main_selling_point":"凉感透气","visual_advice":"浅色夏季户外场景"} for i,(title,copy) in enumerate([("凉感通勤","轻薄不闷汗"),("防晒细节","UPF 防护看得见"),("一衣多穿","通勤户外都清爽")],1)]
    videos=[{"selection_id":f"video-{i}","title":f"防晒衣短视频{i}","hook_3s":"出门三秒穿好防晒","shots":[{"scene_description":"展示上身效果","voiceover":"轻薄凉感，通勤不闷热"}],"cta":"点击查看尺码并立即下单"} for i in range(1,4)]
    return {"image_directions":images,"video_scripts":videos,"selected_image_ids":[],"selected_video_ids":[]}


def model_completion(kind: str, context: dict[str, Any], fallback: dict[str, Any], demo_mode=False) -> dict[str, Any]:
    """Call an OpenAI-compatible text service when configured; otherwise use labelled demo data."""
    with connect() as connection:
        config = row(connection.execute("SELECT * FROM service_configs WHERE id=1").fetchone()) or {}
    if config.get("api_key"):
        fail(503, "legacy_credential", "请由主管重新保存模型配置，以加密已有密钥")
    if demo_mode is True:
        return {**fallback, 'generation_mode':'demo'}
    if not config.get("base_url") or not config.get("api_key_ciphertext") or not config.get("model"):
        fail(503,'model_not_configured','请由主管配置文字模型，或明确选择教学示例模式')
    api_key = decrypt_key(config["api_key_ciphertext"])
    prompt = (
        f"Generate the {kind} as strict JSON for an e-commerce operations teaching app. "
        "Return only JSON, preserve all required fields and counts. Context: "
        + json_value(context) + ' Required structure/example (replace example values with analysis): ' + json_value(fallback)
    )
    endpoint = config["base_url"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": config["model"], "temperature": config.get("temperature", 0.2), "messages": [{"role": "user", "content": prompt}]},
            timeout=float(config.get("request_timeout_seconds", 60)),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("model response must be an object")
        parsed["generation_mode"] = "real"
        return parsed
    except httpx.TimeoutException:
        fail(504,'model_timeout','文字模型请求超时，请重试，已有内容已保留')
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(502, "model_generation_failed", "文字模型调用失败，请检查服务配置或稍后重试")


@app.post("/api/rounds/{round_id}/diagnoses/generate", status_code=201)
def generate_diagnosis(round_id:int, payload:Payload|None=None, user=Depends(require_role("operator"))):
    data=payload.model_dump() if payload else {}
    with connect() as c:
        item=c.execute('SELECT * FROM rounds WHERE id=?',(round_id,)).fetchone()
        if not item: fail(404,'not_found','轮次不存在')
        competitors=[output(dict(x)) for x in c.execute("SELECT * FROM competitors WHERE product_id=? AND verification_status='verified'",(item['product_id'],))]
        if not competitors: fail(409,'missing_competitor','至少需要一份已核对竞品')
        context={'product':get_product(item['product_id'],user),'competitors':competitors,'round':output(dict(item)),'instructions':data.get('instructions','')}
    content=model_completion('diagnosis',context,generated_diagnosis(),data.get('demo_mode',False))
    errors=validate_content('diagnosis',content)
    if errors: fail(502,'invalid_model_content','模型诊断结构不完整',errors)
    with transaction() as c:
        version=c.execute("SELECT COALESCE(MAX(version),0)+1 FROM contents WHERE round_id=? AND kind='diagnosis'",(round_id,)).fetchone()[0]
        cursor=c.execute('INSERT INTO contents(product_id,round_id,kind,version,content) VALUES (?,?,?,?,?)',(item['product_id'],round_id,'diagnosis',version,json_value(content)))
        return output(dict(c.execute('SELECT * FROM contents WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.post("/api/rounds/{round_id}/creatives/generate", status_code=201)
def generate_creative(round_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump(); diagnosis_id=data.get('diagnosis_content_id')
    with connect() as c:
        diagnosis=c.execute("SELECT * FROM contents WHERE id=? AND kind='diagnosis' AND round_id=?",(diagnosis_id,round_id)).fetchone()
        if not diagnosis or diagnosis['status']!='confirmed': fail(409,'diagnosis_not_confirmed','请先确认本轮诊断')
        context={'product':get_product(diagnosis['product_id'],user),'diagnosis':parse_json(diagnosis['content'],{}),'instructions':data.get('instructions','')}
    content=model_completion('creative',context,generated_creative(),data.get('demo_mode',False))
    content.setdefault('selected_image_ids',[]);content.setdefault('selected_video_ids',[])
    errors=validate_content('creative',content)
    if errors: fail(502,'invalid_model_content','模型创意结构或数量不合格',errors)
    with transaction() as c:
        current=c.execute('SELECT status FROM contents WHERE id=?',(diagnosis_id,)).fetchone()
        if current[0]!='confirmed': fail(409,'prerequisite_changed','诊断状态已变化')
        version=c.execute("SELECT COALESCE(MAX(version),0)+1 FROM contents WHERE round_id=? AND kind='creative'",(round_id,)).fetchone()[0]
        cursor=c.execute('INSERT INTO contents(product_id,round_id,kind,version,content,source_content_id) VALUES (?,?,?,?,?,?)',(diagnosis['product_id'],round_id,'creative',version,json_value(content),diagnosis_id))
        return output(dict(c.execute('SELECT * FROM contents WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.get("/api/contents/{content_id}")
def get_content(content_id:int,user=Depends(current_user)):
    with connect() as connection: return output(row(connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone()))


@app.patch("/api/contents/{content_id}", status_code=201)
def patch_content(content_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump(); expected=data.get("expected_version"); content=data.get("content")
    with transaction() as connection:
        old=connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone()
        if not old: fail(404,"not_found","内容不存在")
        if expected != old["version"]: fail(409,"version_conflict","内容版本已变化")
        if old['status']=='archived': fail(409,'archived','内容已归档')
        if isinstance(content,dict): content['generation_mode']=parse_json(old['content'],{}).get('generation_mode','demo')
        errors=validate_content(old["kind"],content or {});
        if errors: fail(422,"invalid_content","内容结构不完整",errors)
        version=connection.execute("SELECT COALESCE(MAX(version),0) FROM contents WHERE round_id=? AND kind=?",(old["round_id"],old["kind"])).fetchone()[0]+1; cursor=connection.execute("INSERT INTO contents(product_id,round_id,kind,version,content,edited_content,source_content_id) VALUES (?,?,?,?,?,?,?)",(old["product_id"],old["round_id"],old["kind"],version,json_value(content),json_value(data.get("edited_content")) if data.get("edited_content") is not None else None,old["id"])); return output(row(connection.execute("SELECT * FROM contents WHERE id=?",(cursor.lastrowid,)).fetchone()))


@app.post("/api/contents/{content_id}/confirm")
def confirm_content(content_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        old=connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone(); expected=payload.model_dump().get("expected_version")
        if not old: fail(404,"not_found","内容不存在")
        if old["version"]!=expected or old["status"]=="archived": fail(409,"version_conflict","版本不可确认")
        errors=validate_content(old["kind"],parse_json(old["content"],{}));
        if errors: fail(422,"invalid_content","内容结构不完整",errors)
        connection.execute("UPDATE contents SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?",(user["id"],now(),content_id)); return output(row(connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone()))


@app.post("/api/contents/{content_id}/archive")
def archive_content(content_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        old=connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone();
        if not old or old["version"]!=payload.model_dump().get("expected_version"): fail(409,"version_conflict","版本不可归档")
        connection.execute("UPDATE contents SET status='archived',archived_at=? WHERE id=?",(now(),content_id)); return output(row(connection.execute("SELECT * FROM contents WHERE id=?",(content_id,)).fetchone()))


def task_payload(item: dict[str, Any]) -> dict[str, Any]:
    if not item: fail(404,"not_found","任务不存在")
    with connect() as connection:
        return task_service.public_task(item, connection)


@app.get("/api/tasks")
def list_tasks(product_id:int|None=None,round_id:int|None=None,status:str|None=None,task_type:str|None=None,user=Depends(current_user)):
    where=[]; args=[]
    for key,val in (("product_id",product_id),("round_id",round_id),("status",status),("task_type",task_type)):
        if val is not None: where.append(f"{key}=?"); args.append(val)
    clause=f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as connection: rows=[task_payload(output(row(x))) for x in connection.execute(f"SELECT * FROM tasks {clause} ORDER BY id DESC",args)]
    return page(rows,len(rows),1,len(rows) or 50)


@app.get("/api/tasks/{task_id}")
def get_task(task_id:int,user=Depends(current_user)):
    with connect() as connection: return task_payload(output(row(connection.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())))


@app.post("/api/tasks",status_code=202)
def create_task(payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump(); required=("creative_content_id","selection_id","task_type")
    if any(not data.get(x) for x in required) or data["task_type"] not in {"image","video"}: fail(422,"invalid_task","任务字段无效")
    with transaction() as connection:
        creative=connection.execute("SELECT * FROM contents WHERE id=? AND kind='creative' AND status='confirmed'",(data["creative_content_id"],)).fetchone()
        if not creative: fail(409,"creative_not_confirmed","请先确认创意")
        content=parse_json(creative["content"],{}); pool=content.get("image_directions" if data["task_type"]=="image" else "video_scripts",[])
        if not any(x.get("selection_id")==data["selection_id"] for x in pool): fail(422,"selection_not_found","选定创意不存在")
        selected = content.get('selected_image_ids' if data['task_type']=='image' else 'selected_video_ids', [])
        if data['selection_id'] not in selected: fail(422,'selection_not_confirmed','该方案未被选定')
        scenario=data.get('demo_scenario','success')
        if scenario not in {'success','fail_once','always_fail','timeout','late_result'}: fail(422,'invalid_scenario','演示情形无效')
        task_id=task_service.enqueue(connection, creative, data['selection_id'], data['task_type'], scenario)
    with connect() as connection: return task_payload(output(row(connection.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())))




@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id:int,payload:Payload|None=None,user=Depends(require_role("operator"))):
    with task_service.queue_transaction() as connection:
        item=connection.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not item: fail(404,"not_found","任务不存在")
        if item["status"] not in {"pending","running"}: fail(409,"task_finished","任务已结束")
        reason=(payload.model_dump().get('reason') if payload else None) or '运营主动取消'
        connection.execute("UPDATE tasks SET status='cancelled',cancel_reason=?,cancelled_at=?,finished_at=?,updated_at=? WHERE id=?",(reason,now(),now(),now(),task_id))
        if item['current_attempt_id']:
            connection.execute("UPDATE task_attempts SET status='cancelled',finished_at=?,error_reason=? WHERE id=?",(now(),reason,item['current_attempt_id']))
        result=dict(connection.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
    return task_payload(result)


@app.post("/api/tasks/{task_id}/retry",status_code=202)
def retry_task(task_id:int,user=Depends(require_role("operator"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not item or item["status"] not in {"failed","timeout"} or item["attempts"]<=item["max_auto_retries"]: fail(409,"retry_unavailable","当前任务不可人工重试")
        creative=connection.execute('SELECT * FROM contents WHERE id=?',(item['creative_content_id'],)).fetchone()
        new_id=task_service.enqueue(connection,creative,item['selection_id'],item['task_type'],'success',task_id)
    with connect() as connection: return task_payload(output(row(connection.execute("SELECT * FROM tasks WHERE id=?",(new_id,)).fetchone())))


@app.get("/api/materials")
def list_materials(product_id:int|None=None,round_id:int|None=None,review_status:str|None=None,include_archived:bool=False,user=Depends(current_user)):
    where=[];args=[]
    for key,val in (("product_id",product_id),("round_id",round_id),("review_status",review_status)):
        if val is not None: where.append(f"{key}=?");args.append(val)
    if not include_archived: where.append("archived_at IS NULL")
    clause=f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as connection: rows=[output(row(x)) for x in connection.execute(f"SELECT * FROM materials {clause} ORDER BY id DESC",args)]
    return page(rows,len(rows),1,len(rows) or 50)


@app.get("/api/materials/{material_id}")
def get_material(material_id:int,user=Depends(current_user)):
    with connect() as connection: return output(row(connection.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone()))


@app.post("/api/materials/{material_id}/review")
def review_material(material_id:int,payload:ReviewPayload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        material=connection.execute('SELECT * FROM materials WHERE id=?',(material_id,)).fetchone()
        if not material: fail(404,'not_found','素材不存在')
        if material['archived_at']: fail(409,'archived','素材已归档')
        history=parse_json(material.get('review_history'),[]) or []
        history.append({'decision':payload.decision,'comment':payload.comment,'reviewed_by':user['id'],'reviewed_at':now()})
        connection.execute('UPDATE materials SET review_history=? WHERE id=?',(json_value(history),material_id))
        connection.execute("UPDATE materials SET review_status=?,reviewed_by=?,reviewed_at=? WHERE id=?",(payload.decision,payload and user["id"],now(),material_id)); return output(row(connection.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone()))


@app.patch("/api/materials/{material_id}/tags")
def tags_material(material_id:int,payload:Payload,user=Depends(require_role("operator"))):
    tags=payload.model_dump().get("tags",[])
    if not isinstance(tags,list): fail(422,"invalid_tags","标签必须为数组")
    with transaction() as connection: connection.execute("UPDATE materials SET tags=? WHERE id=?",(json_value(tags),material_id)); return output(row(connection.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone()))


@app.post("/api/materials/{material_id}/archive")
def archive_material(material_id:int,user=Depends(require_role("operator"))):
    with transaction() as connection: connection.execute("UPDATE materials SET archived_at=? WHERE id=?",(now(),material_id)); return output(row(connection.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone()))


@app.get("/api/materials/file/{task_id}")
def material_file(task_id:int,user=Depends(current_user)):
    with connect() as connection: item=connection.execute("SELECT * FROM materials WHERE task_id=?",(task_id,)).fetchone()
    if not item: fail(404,"not_found","素材不存在")
    ext="png" if item["material_type"]=="image" else "mp4"; path=Path(item['file_path']) if item.get('file_path') else STORAGE/"materials"/f"task-{task_id}.{ext}"
    if not path.exists(): fail(404,"file_missing","素材文件不存在")
    return FileResponse(path,media_type="image/png" if ext=="png" else "video/mp4")


def proposal_output(item: dict[str,Any]) -> dict[str,Any]:
    item=output(item); item["content_confirmed"]=bool(item.get("content_confirmed_at")); return item


@app.post("/api/rounds/{round_id}/ad-proposals/generate",status_code=201)
def generate_proposal(round_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump()
    with connect() as c:
        item=c.execute('SELECT * FROM rounds WHERE id=?',(round_id,)).fetchone()
        if not item: fail(404,'not_found','轮次不存在')
        referenced={}
        for kind,key in [('diagnosis','diagnosis_content_id'),('creative','creative_content_id')]:
            content=c.execute('SELECT * FROM contents WHERE id=?',(data.get(key),)).fetchone()
            if not content or content['kind']!=kind or content['round_id']!=round_id or content['product_id']!=item['product_id'] or content['status']!='confirmed': fail(409,'proposal_prerequisite','请引用本商品本轮已确认诊断和创意')
            referenced[kind]=output(dict(content))
        context={'product':get_product(item['product_id'],user),'request':data,**referenced}
    fallback={'strategy':{'target_audience':['夏季通勤人群'],'audience_packages':['教学定向人群'],'bid':'1.00','budget':'4200.00','description':'课堂模拟人工投放'},'groups':[{'group_code':code,'material_ids':data.get('material_ids',[]),'audience':{'description':'夏季通勤'},'bid':'1.00','budget':'2100.00','targets':data.get('targets',{})} for code in ('A','B')]}
    generated=model_completion('ad_proposal',context,fallback,data.get('demo_mode',False))
    data.update(strategy=generated.get('strategy',{}),groups=generated.get('groups',[]))
    data['strategy']['generation_mode']=generated['generation_mode']
    with transaction() as c:
        workflow.validate_proposal(c,item['product_id'],round_id,data)
        version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE round_id=?',(round_id,)).fetchone()[0]
        cursor=c.execute('INSERT INTO proposals(product_id,round_id,shop_id,proposal_key,version,strategy,groups_data,targets,material_ids,date_from,date_to) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(item['product_id'],round_id,data['shop_id'],f"round-{round_id}-shop-{data['shop_id']}",version,json_value(data['strategy']),json_value(data['groups']),json_value(data['targets']),json_value(data['material_ids']),data['date_from'],data['date_to']))
        c.execute('UPDATE proposals SET diagnosis_content_id=?,creative_content_id=?,content_snapshot=? WHERE id=?',(data['diagnosis_content_id'],data['creative_content_id'],json_value(context),cursor.lastrowid))
        return proposal_output(dict(c.execute('SELECT * FROM proposals WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.get("/api/ad-proposals/{proposal_id}")
def get_proposal(proposal_id:int,user=Depends(current_user)):
    with connect() as c:
        item=proposal_output(row(c.execute('SELECT * FROM proposals WHERE id=?',(proposal_id,)).fetchone()))
        item['approvals']=[output(dict(x)) for x in c.execute('SELECT * FROM approvals WHERE proposal_id=? ORDER BY id',(proposal_id,))]
        return item


@app.patch("/api/ad-proposals/{proposal_id}",status_code=201)
def patch_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump()
    with transaction() as c:
        old=c.execute('SELECT * FROM proposals WHERE id=?',(proposal_id,)).fetchone()
        if not old or old['version']!=data.get('expected_version') or old['archived_at']: fail(409,'version_conflict','建议版本不可编辑')
        merged={key:data.get(key,parse_json(old[key],{}) if key in {'strategy','targets','material_ids'} else old[key]) for key in ('shop_id','strategy','targets','material_ids','date_from','date_to')}
        merged['groups']=data.get('groups',parse_json(old['groups_data'],[]))
        workflow.validate_proposal(c,old['product_id'],old['round_id'],merged)
        version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE proposal_key=?',(old['proposal_key'],)).fetchone()[0]
        cursor=c.execute('INSERT INTO proposals(product_id,round_id,shop_id,proposal_key,version,strategy,groups_data,targets,material_ids,date_from,date_to) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(old['product_id'],old['round_id'],merged['shop_id'],old['proposal_key'],version,json_value(merged['strategy']),json_value(merged['groups']),json_value(merged['targets']),json_value(merged['material_ids']),merged['date_from'],merged['date_to']))
        c.execute('UPDATE proposals SET diagnosis_content_id=?,creative_content_id=?,content_snapshot=? WHERE id=?',(old['diagnosis_content_id'],old['creative_content_id'],old['content_snapshot'],cursor.lastrowid))
        return proposal_output(dict(c.execute('SELECT * FROM proposals WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.post("/api/ad-proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone();
        if not item or item["version"]!=payload.model_dump().get("expected_version") or item["status"]!="draft": fail(409,"proposal_state","建议当前不可确认")
        connection.execute("UPDATE proposals SET content_confirmed_by=?,content_confirmed_at=? WHERE id=?",(user["id"],now(),proposal_id)); return proposal_output(row(connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone()))


@app.post("/api/ad-proposals/{proposal_id}/submit")
def submit_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone();
        if not item or item["version"]!=payload.model_dump().get("expected_version") or not item["content_confirmed_at"] or item['status']!='draft' or item['archived_at']: fail(409,"proposal_not_confirmed","请先确认可提交版本的建议内容")
        connection.execute("UPDATE proposals SET status='pending_approval' WHERE id=?",(proposal_id,)); return proposal_output(row(connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone()))


@app.post("/api/ad-proposals/{proposal_id}/approve",status_code=201)
def approve_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("manager"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone();
        if not item or item["status"]!="pending_approval" or item['archived_at'] or item['version']!=payload.model_dump().get('expected_version'): fail(409,"proposal_state","建议不在可审批版本的待审批状态")
        claimed=connection.execute("UPDATE proposals SET status='approved' WHERE id=? AND status='pending_approval' AND archived_at IS NULL",(proposal_id,))
        if claimed.rowcount!=1: fail(409,'already_decided','建议已由其他请求审批')
        connection.execute("UPDATE proposals SET status='approved' WHERE id=?",(proposal_id,)); approval_cursor=connection.execute("INSERT INTO approvals(proposal_id,decision,comment,actor_id) VALUES (?,?,?,?)",(proposal_id,"approved",payload.model_dump().get("comment",""),user["id"])); cursor=connection.execute("INSERT INTO experiments(proposal_id,shop_id,product_id,date_from,date_to,targets) VALUES (?,?,?,?,?,?)",(proposal_id,item["shop_id"],item["product_id"],item["date_from"],item["date_to"],item["targets"])); return {"proposal":proposal_output(row(connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone())),"approval":output(row(connection.execute("SELECT * FROM approvals WHERE id=?",(approval_cursor.lastrowid,)).fetchone())),"experiment":output(row(connection.execute("SELECT * FROM experiments WHERE id=?",(cursor.lastrowid,)).fetchone()))}


@app.post("/api/ad-proposals/{proposal_id}/reject")
def reject_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("manager"))):
    comment=payload.model_dump().get("comment","");
    if not comment: fail(422,"missing_comment","驳回意见不能为空")
    with transaction() as connection:
        item=connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone();
        if not item or item["status"]!="pending_approval" or item['archived_at'] or item['version']!=payload.model_dump().get('expected_version'): fail(409,"proposal_state","建议不在可审批版本的待审批状态")
        claimed=connection.execute("UPDATE proposals SET status='rejected' WHERE id=? AND status='pending_approval' AND archived_at IS NULL",(proposal_id,))
        if claimed.rowcount!=1: fail(409,'already_decided','建议已由其他请求审批')
        connection.execute("UPDATE proposals SET status='rejected' WHERE id=?",(proposal_id,)); cursor=connection.execute("INSERT INTO approvals(proposal_id,decision,comment,actor_id) VALUES (?,?,?,?)",(proposal_id,"rejected",comment,user["id"])); return {"proposal":proposal_output(row(connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone())),"approval":output(row(connection.execute("SELECT * FROM approvals WHERE id=?",(cursor.lastrowid,)).fetchone()))}


@app.post("/api/ad-proposals/{proposal_id}/archive")
def archive_proposal(proposal_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone();
        if not item or item["version"]!=payload.model_dump().get("expected_version"): fail(409,"version_conflict","建议版本已变化")
        connection.execute("UPDATE proposals SET archived_at=?,status=? WHERE id=?",(now(),'approved' if item['status']=='approved' else 'archived',proposal_id)); return proposal_output(row(connection.execute("SELECT * FROM proposals WHERE id=?",(proposal_id,)).fetchone()))


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id:int,user=Depends(current_user)):
    with connect() as connection:
        item=output(row(connection.execute('SELECT * FROM experiments WHERE id=?',(experiment_id,)).fetchone()))
        proposal=connection.execute('SELECT * FROM proposals WHERE id=?',(item['proposal_id'],)).fetchone()
        item.update(experiment_code=f'EXP-{experiment_id}',round_id=proposal['round_id'],approved_proposal_id=proposal['id'],groups=parse_json(proposal['groups_data'],[]),strategy=parse_json(proposal['strategy'],{}),material_ids=parse_json(proposal['material_ids'],[]))
    return item


@app.patch("/api/experiments/{experiment_id}/execution-note")
def patch_note(experiment_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection: connection.execute("UPDATE experiments SET execution_note=? WHERE id=?",(payload.model_dump().get("execution_note",""),experiment_id)); return output(row(connection.execute("SELECT * FROM experiments WHERE id=?",(experiment_id,)).fetchone()))


def metric_values(rows: list[dict[str,Any]]) -> dict[str,Any]:
    total={k:sum((r[k] or 0) for r in rows) for k in ("impressions","clicks","orders")}
    for key in ('gmv', 'ad_spend'):
        total[key] = sum((Decimal(str(r[key] or 0)) for r in rows), Decimal('0'))
    visits=sum(r.get("detail_visits") or 0 for r in rows if r.get("detail_visits") is not None); bounces=sum(r.get("detail_bounces") or 0 for r in rows if r.get("detail_bounces") is not None); total["detail_visits"]=visits;total["detail_bounces"]=bounces
    detail_complete = all(r.get("detail_visits") is not None and r.get("detail_bounces") is not None for r in rows) if rows else False
    return {"totals":{**total, 'gmv': format(total['gmv'], '.2f'), 'ad_spend': format(total['ad_spend'], '.2f')},"metrics":{"ctr":total["clicks"]/total["impressions"] if total["impressions"] else None,"conversion_rate":total["orders"]/total["clicks"] if total["clicks"] else None,"roi":float(total["gmv"]/total["ad_spend"]) if total["ad_spend"] else None,"detail_bounce_rate":bounces/visits if visits and detail_complete else None}}


@app.get("/api/experiments/{experiment_id}/metrics")
def list_metrics(experiment_id:int,user=Depends(current_user)):
    with connect() as connection: rows=[output(row(x)) for x in connection.execute("SELECT * FROM metrics WHERE experiment_id=? ORDER BY date,group_code",(experiment_id,))]
    return {"items":rows,"total":len(rows),**metric_values(rows)}


@app.post("/api/experiments/{experiment_id}/metrics",status_code=201)
def create_metric(experiment_id:int,payload:MetricPayload,user=Depends(require_role("operator"))):
    if payload.detail_bounces is not None and payload.detail_visits is not None and payload.detail_bounces>payload.detail_visits: fail(422,"invalid_detail_metrics","跳失次数不能超过访问次数")
    with transaction() as connection:
        experiment=connection.execute("SELECT * FROM experiments WHERE id=?",(experiment_id,)).fetchone();
        if not experiment: fail(404,"not_found","实验不存在")
        workflow.validate_metric(connection,experiment_id,payload.model_dump())
        if not (experiment["date_from"]<=str(payload.date)<=experiment["date_to"]): fail(422,"date_out_of_range","日期不在实验周期")
        try:
            cursor=connection.execute("INSERT INTO metrics(experiment_id,date,group_code,impressions,clicks,orders,gmv,ad_spend,detail_visits,detail_bounces) VALUES (?,?,?,?,?,?,?,?,?,?)",(experiment_id,str(payload.date),payload.group_code,payload.impressions,payload.clicks,payload.orders,payload.gmv,payload.ad_spend,payload.detail_visits,payload.detail_bounces))
        except Exception as exc:
            if is_duplicate(exc): fail(409,"duplicate_metric","同日期/实验/组别数据已存在")
            raise
        item=output(row(connection.execute("SELECT * FROM metrics WHERE id=?",(cursor.lastrowid,)).fetchone())); return {**item,**metric_values([item])["metrics"]}


@app.patch("/api/metrics/{metric_id}")
def patch_metric(metric_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data={k:v for k,v in payload.model_dump().items() if k in {"impressions","clicks","orders","gmv","ad_spend","detail_visits","detail_bounces"}}
    with transaction() as connection:
        old=connection.execute("SELECT * FROM metrics WHERE id=?",(metric_id,)).fetchone();
        if not old: fail(404,"not_found","经营数据不存在")
        merged=dict(old);merged.update(data)
        checked=MetricPayload.model_validate(merged).model_dump()
        workflow.validate_metric(connection,old['experiment_id'],checked)
        data={key:checked[key] for key in data}
        if not data: fail(422,'empty_patch','没有可修改经营字段')
        if merged.get("detail_bounces") is not None and merged.get("detail_visits") is not None and merged["detail_bounces"]>merged["detail_visits"]: fail(422,"invalid_detail_metrics","跳失次数不能超过访问次数")
        sets=",".join(f"{k}=?" for k in data); connection.execute(f"UPDATE metrics SET {sets} WHERE id=?",(*data.values(),metric_id)); item=output(row(connection.execute("SELECT * FROM metrics WHERE id=?",(metric_id,)).fetchone())); return {**item,**metric_values([item])["metrics"]}


def report_sections(product_id:int, experiment_id:int, metrics:dict[str,Any]):
    return {"product_overview":{"product_id":product_id},"diagnosis_summary":{"target_audience":"夏季通勤及户外防晒人群","price_competitiveness":"299 元位于主流价格带"},"creative_assets":{"note":"引用已审核素材版本"},"experiment_record":{"experiment_id":experiment_id},"performance_review":{"metrics":metrics,"hypotheses":["面料说明不足导致跳失偏高（待验证）"],"actions":[{"action_id":"fabric-detail","title":"补充面料特写说明"}]}}


@app.post("/api/experiments/{experiment_id}/close-period",status_code=201)
def close_period(experiment_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump()
    with connect() as c:
        exp=c.execute('SELECT * FROM experiments WHERE id=?',(experiment_id,)).fetchone()
        if not exp: fail(404,'not_found','实验不存在')
        scope={'date_from':data.get('date_from',exp['date_from']),'date_to':data.get('date_to',exp['date_to'])}
        if not exp['date_from']<=scope['date_from']<=scope['date_to']<=exp['date_to']: fail(422,'invalid_range','复盘范围须在实验周期内')
        if date.fromisoformat(exp['date_to'])>=date.today(): fail(409,'period_not_finished','实验周期尚未结束')
        rows=[output(dict(x)) for x in c.execute('SELECT * FROM metrics WHERE experiment_id=? AND date BETWEEN ? AND ?',(experiment_id,scope['date_from'],scope['date_to']))]
        if not rows: fail(409,'missing_metrics','所选范围没有经营数据')
        context=workflow.report_context(c,exp,rows,metric_values(rows),scope)
    generated=model_completion('review',context,workflow.demo_report(context),data.get('demo_mode',False))
    workflow.validate_report(generated.get('sections'),generated.get('actions'))
    with transaction() as c:
        round_id=context['proposal']['round_id']
        version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM reviews WHERE round_id=?',(round_id,)).fetchone()[0]
        snapshot={**context,'generation_mode':generated['generation_mode']}
        cursor=c.execute('INSERT INTO reviews(product_id,round_id,experiment_id,version,sections,actions,snapshot) VALUES (?,?,?,?,?,?,?)',(exp['product_id'],round_id,experiment_id,version,json_value(generated['sections']),json_value(generated['actions']),json_value(snapshot)))
        return output(dict(c.execute('SELECT * FROM reviews WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.post("/api/experiments/{experiment_id}/reviews/generate",status_code=201)
def generate_review(experiment_id:int,user=Depends(require_role("operator"))):
    return close_period(experiment_id,Payload(),user)


@app.get("/api/reviews/{review_id}")
def get_review(review_id:int,user=Depends(current_user)):
    with connect() as connection: return output(row(connection.execute("SELECT * FROM reviews WHERE id=?",(review_id,)).fetchone()))


@app.patch("/api/reviews/{review_id}",status_code=201)
def patch_review(review_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        old=connection.execute("SELECT * FROM reviews WHERE id=?",(review_id,)).fetchone(); expected=payload.model_dump().get("expected_version")
        if not old or old["version"]!=expected: fail(409,"version_conflict","复盘版本已变化")
        if old['status']=='archived': fail(409,'archived','报告已归档')
        sections=payload.model_dump().get('sections',parse_json(old['sections'])); actions=payload.model_dump().get('actions',parse_json(old['actions']))
        workflow.validate_report(sections,actions)
        version=connection.execute('SELECT COALESCE(MAX(version),0)+1 FROM reviews WHERE round_id=?',(old['round_id'],)).fetchone()[0]
        cursor=connection.execute('INSERT INTO reviews(product_id,round_id,experiment_id,version,sections,actions,snapshot) VALUES (?,?,?,?,?,?,?)',(old['product_id'],old['round_id'],old['experiment_id'],version,json_value(sections),json_value(actions),old['snapshot']))
        return output(row(connection.execute('SELECT * FROM reviews WHERE id=?',(cursor.lastrowid,)).fetchone()))


@app.post("/api/reviews/{review_id}/confirm")
def confirm_review(review_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection:
        item=connection.execute("SELECT * FROM reviews WHERE id=?",(review_id,)).fetchone();
        if not item or item["version"]!=payload.model_dump().get("expected_version") or item['status']=='archived': fail(409,"version_conflict","复盘版本已变化")
        workflow.validate_report(parse_json(item['sections']),parse_json(item['actions']))
        connection.execute("UPDATE reviews SET status='confirmed',confirmed_at=? WHERE id=?",(now(),review_id)); return output(row(connection.execute("SELECT * FROM reviews WHERE id=?",(review_id,)).fetchone()))


@app.post("/api/reviews/{review_id}/archive")
def archive_review(review_id:int,payload:Payload,user=Depends(require_role("operator"))):
    with transaction() as connection: connection.execute("UPDATE reviews SET status='archived',archived_at=? WHERE id=?",(now(),review_id)); return output(row(connection.execute("SELECT * FROM reviews WHERE id=?",(review_id,)).fetchone()))


@app.post("/api/reviews/{review_id}/next-round",status_code=201)
def next_round(review_id:int,payload:Payload,user=Depends(require_role("operator"))):
    data=payload.model_dump()
    with transaction() as c:
        review=c.execute("SELECT * FROM reviews WHERE id=? AND status='confirmed'",(review_id,)).fetchone()
        if not review or review['version']!=data.get('expected_version'): fail(409,'review_not_confirmed','请使用已确认复盘版本')
        actions=parse_json(review['actions'],[]); chosen=data.get('action_ids',[])
        if not isinstance(chosen,list) or not chosen or len(set(chosen))!=len(chosen) or any(x not in {a['action_id'] for a in actions} for x in chosen): fail(422,'invalid_actions','请选择报告内优化行动')
        if c.execute('SELECT 1 FROM rounds WHERE source_report_id=?',(review_id,)).fetchone(): fail(409,'round_exists','已创建下一轮')
        selected=[a for a in actions if a['action_id'] in chosen]
        number=c.execute('SELECT COALESCE(MAX(round_no),0)+1 FROM rounds WHERE product_id=?',(review['product_id'],)).fetchone()[0]
        cursor=c.execute('INSERT INTO rounds(product_id,round_no,source_report_id,selected_actions,phase) VALUES (?,?,?,?,?)',(review['product_id'],number,review_id,json_value(selected),'diagnosis'))
        round_item=output(dict(c.execute('SELECT * FROM rounds WHERE id=?',(cursor.lastrowid,)).fetchone()))
    diagnosis=generate_diagnosis(round_item['id'],Payload(demo_mode=data.get('demo_mode',False)),user)
    return {'round':round_item,'diagnosis':diagnosis}


@app.get("/api/dashboard")
def dashboard(shop_id:int|None=None,date_from:str|None=None,date_to:str|None=None,user=Depends(current_user)):
    start=date_from or str(date.today()-timedelta(days=6)); end=date_to or str(date.today())
    try:
        if date.fromisoformat(start)>date.fromisoformat(end): fail(422,'invalid_range','日期范围无效')
    except ValueError: fail(422,'invalid_date','请输入有效日期')
    with connect() as c:
        product_count=c.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        args=[start,end]; clause=''
        if shop_id: clause=' AND e.shop_id=?'; args.append(shop_id)
        rows=[output(dict(x)) for x in c.execute('SELECT m.*,e.product_id,e.shop_id FROM metrics m JOIN experiments e ON e.id=m.experiment_id WHERE m.date BETWEEN ? AND ?'+clause,args)]
        alerts=[output(dict(x)) for x in c.execute('SELECT m.*,l.shop_id,l.product_id FROM sku_mappings m JOIN listings l ON l.id=m.listing_id WHERE m.stock_quantity<=m.warning_threshold'+(' AND l.shop_id=?' if shop_id else ''),[shop_id] if shop_id else [])]
        on_sale={x[0] for x in c.execute('SELECT DISTINCT product_id FROM listings WHERE on_sale=1'+(' AND shop_id=?' if shop_id else ''),[shop_id] if shop_id else [])}
        active={x['product_id'] for x in rows if x['orders']>0 and x['product_id'] in on_sale}
        shops=[]
        for shop in c.execute('SELECT * FROM shops').fetchall():
            ids={x[0] for x in c.execute('SELECT product_id FROM listings WHERE shop_id=?',(shop['id'],))}
            shops.append({**output(dict(shop)),'product_count':len(ids)})
    trend=[{'date':day,**metric_values([x for x in rows if x['date']==day])} for day in sorted({x['date'] for x in rows})]
    return {'date_from':start,'date_to':end,'product_count':product_count,'on_sale_product_count':len(on_sale),'active_product_count':len(active),'sell_through_rate':len(active)/len(on_sale) if on_sale else None,**metric_values(rows),'trend':trend,'inventory_alerts':alerts,'shops':shops}


@app.get("/api/imports/templates/{import_type}")
def import_template(import_type:str,format:str="csv",user=Depends(current_user)):
    content,media_type=import_service.template(import_type,format)
    return Response(content=content,media_type=media_type,headers={"Content-Disposition":f"attachment; filename={import_type}.{format}"})


@app.post('/api/imports/inspect')
def inspect_import(file:UploadFile=File(...),sheet_name:str|None=Form(None),user=Depends(require_role('operator'))):
    headers,values,sheets,selected=import_service.parse_file(file.filename,file.file.read(5*1024*1024+1),sheet_name)
    return {'headers':headers,'sheets':sheets,'sheet_name':selected,'row_count':len(values),'sample':[[str(v) if v is not None else '' for v in row] for row in values[:5]],'fields':import_service.FIELDS}

@app.post("/api/imports/preview",status_code=201)
def import_preview(import_type:str=Form(...),file:UploadFile=File(...),field_mapping:str=Form('{}'),sheet_name:str|None=Form(None),user=Depends(require_role("operator"))):
    try: mapping=json.loads(field_mapping)
    except ValueError: fail(422,'invalid_mapping','字段映射必须为 JSON 对象')
    batch,headers,sheets=import_service.preview(import_type,file.filename,file.file.read(5*1024*1024+1),mapping,sheet_name)
    result=get_import(batch,user=user); result.update(headers=headers,sheets=sheets)
    return result


@app.get("/api/imports/{import_id}")
def get_import(import_id:int,page_no:int=Query(1,alias='page',ge=1),page_size:int=Query(50,ge=1,le=200),user=Depends(current_user)):
    with connect() as connection: item=connection.execute("SELECT * FROM imports WHERE id=?",(import_id,)).fetchone()
    if not item: fail(404,"not_found","导入批次不存在")
    rows=parse_json(item["rows_data"],[])
    if not isinstance(page_no,int): page_no=1
    if not isinstance(page_size,int): page_size=50
    return {"id":item["id"],"import_type":item["import_type"],"filename":item["filename"],"status":item["status"],"field_mapping":parse_json(item['field_mapping'],{}),"sheet_name":item['sheet_name'],"rows":page(rows[(page_no-1)*page_size:page_no*page_size],len(rows),page_no,page_size),"summary":parse_json(item["summary"],{})}


@app.post("/api/imports/{import_id}/commit")
def commit_import(import_id:int,payload:ImportCommit,user=Depends(require_role("operator"))):
    return import_service.commit(import_id,payload.row_resolutions,user['id'])
