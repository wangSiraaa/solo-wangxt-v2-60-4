"""OpenAPI 3.0.3 规范（与 web.py 路由一一对应，由 /api/v1/openapi 原样提供）。"""


def _error(status: str) -> dict:
    return {"description": {"400": "请求非法", "404": "对象不存在",
                            "409": "冲突（互斥/超卖/重复预约/幂等冲突/开工阻断等）"}[status],
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


_ID_PARAM = lambda name="id": {
    "name": name, "in": "path", "required": True, "schema": {"type": "integer"}}

_IDEMP_PARAM = {"in": "header", "name": "Idempotency-Key", "required": False,
                "schema": {"type": "string", "maxLength": 128},
                "description": "幂等键；相同键+相同请求体重放返回首次结果且不重复占量"}

_COMPONENTS = {
    "schemas": {
        "Error": {
            "type": "object",
            "required": ["error"],
            "properties": {"error": {"type": "object", "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "details": {"type": "array", "items": {}}}}},
        },
        "Resource": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "example": "OXYGEN_KIT"},
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["MATERIAL", "EQUIPMENT"]},
                "online": {"type": "boolean", "description": "资源是否上线（false=维修/失效）"},
                "status": {"type": "string", "enum": ["AVAILABLE", "OCCUPIED", "MAINTENANCE"],
                           "description": "推导状态：维修下线 > 有占用 > 可用"},
                "total_units": {"type": "integer"},
                "maintenance_units": {"type": "integer"},
                "occupied_units": {"type": "integer"},
                "available_now": {"type": "integer"},
                "version": {"type": "integer"},
                "updated_at": {"type": "string", "format": "date-time"}},
        },
        "ResourceUnit": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "resource_code": {"type": "string"},
                "seq": {"type": "integer"},
                "status": {"type": "string", "enum": ["AVAILABLE", "OCCUPIED", "MAINTENANCE"]}},
        },
        "Plan": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "section": {"type": "string", "example": "SECTION_A"},
                "window_start": {"type": "string", "format": "date-time"},
                "window_end": {"type": "string", "format": "date-time"},
                "status": {"type": "string",
                           "enum": ["SCHEDULED", "IN_PROGRESS", "COMPLETED", "CANCELLED"]}},
        },
        "ReservationItem": {
            "type": "object",
            "required": ["resource_code", "quantity"],
            "properties": {
                "resource_code": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "window_start": {"type": "string", "format": "date-time",
                                 "description": "可选，缺省取计划窗口起点"},
                "window_end": {"type": "string", "format": "date-time",
                               "description": "可选，缺省取计划窗口终点"}},
        },
        "ReservationLine": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "resource_code": {"type": "string"},
                "quantity": {"type": "integer"},
                "window_start": {"type": "string", "format": "date-time"},
                "window_end": {"type": "string", "format": "date-time"},
                "status": {"type": "string", "enum": ["HELD", "REPLACED", "RELEASED"]},
                "replaces_line_id": {"type": "integer", "nullable": True},
                "unit_ids": {"type": "array", "items": {"type": "integer"}}},
        },
        "Blocker": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "resource_code": {"type": "string"},
                "reason": {"type": "string", "enum": ["RESOURCE_OFFLINE", "UNIT_MAINTENANCE"]},
                "message": {"type": "string"},
                "unit_id": {"type": "integer", "nullable": True}},
        },
    },
    "parameters": {"IdempotencyKey": _IDEMP_PARAM},
    "responses": {
        "Error400": _error("400"),
        "Error404": _error("404"),
        "Error409": _error("409"),
    },
}


_PLAN_TIME_BODY = {
    "type": "object",
    "required": ["name", "section", "window_start", "window_end"],
    "properties": {
        "name": {"type": "string"},
        "section": {"type": "string"},
        "window_start": {"type": "string", "format": "date-time"},
        "window_end": {"type": "string", "format": "date-time"},
    },
}

_RESERVE_BODY = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {"type": "array", "minItems": 1,
                  "items": {"$ref": "#/components/schemas/ReservationItem"}},
    },
}

_REPLACE_BODY = {
    "type": "object",
    "properties": {
        "line_id": {"type": "integer"},
        "old_unit_id": {"type": "integer", "description": "同资源换机时提供"},
        "new_unit_id": {"type": "integer", "description": "同资源换机时提供"},
        "replacement_resource_code": {"type": "string", "description": "替代资源时提供"},
        "quantity": {"type": "integer", "minimum": 1},
    },
}

_READINESS_RESPONSE = {
    "200": {"description": "OK", "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "ready": {"type": "boolean"},
            "blockers": {"type": "array",
                         "items": {"$ref": "#/components/schemas/Blocker"}},
        }}}}},
    "404": {"$ref": "#/components/responses/Error404"},
}


OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "应急演练 · 防护物资/演练设备预约 API（本地模拟）",
        "version": "1.0.0",
        "description": (
            "在既有区段互斥矩阵之外，新增本地模拟的防护物资/演练设备预约能力。"
            "即使两个演练计划在互斥矩阵中允许并行，稀缺资源也不会超卖。\n\n"
            "核心语义：\n"
            "- 占用窗口使用半开区间 [window_start, window_end)，首尾相接窗口可复用；\n"
            "- 并发预约通过 BEGIN IMMEDIATE 写串行化 + 数据库触发器双重防超卖；\n"
            "- POST /reservations 支持 Idempotency-Key，重复/重试不重复占量；\n"
            "- 销记 POST /release 仅释放一次，重复调用返回 already_released=true；\n"
            "- 开工前资源/实例维修下线会被逐项阻断，替换后可恢复开工；\n"
            "- 所有关键动作写入 append-only 审计日志。"
        ),
    },
    "servers": [{"url": "/"}],
    "tags": [
        {"name": "meta", "description": "健康检查/规范/区段矩阵"},
        {"name": "resources", "description": "防护物资与演练设备"},
        {"name": "plans", "description": "演练计划与资源预约"},
        {"name": "audit", "description": "审计日志"},
    ],
    "components": _COMPONENTS,
    "paths": {
        "/api/v1/health": {
            "get": {"tags": ["meta"], "summary": "健康检查",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/v1/openapi": {
            "get": {"tags": ["meta"], "summary": "OpenAPI 规范",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/v1/sections": {
            "get": {"tags": ["meta"], "summary": "区段与互斥矩阵（既有规则）",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/v1/resources": {
            "get": {"tags": ["resources"], "summary": "资源清单与库存",
                    "responses": {"200": {"description": "OK"}}},
            "post": {
                "tags": ["resources"],
                "summary": "登记资源（含初始实例数量）",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["code", "name", "kind", "quantity"],
                    "properties": {
                        "code": {"type": "string"},
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": ["MATERIAL", "EQUIPMENT"]},
                        "quantity": {"type": "integer", "minimum": 1}},
                }}}},
                "responses": {
                    "201": {"description": "已创建"},
                    "400": {"$ref": "#/components/responses/Error400"},
                    "409": {"$ref": "#/components/responses/Error409"}},
            },
        },
        "/api/v1/resources/{code}": {
            "parameters": [_ID_PARAM("code")],
            "get": {"tags": ["resources"], "summary": "资源详情/库存",
                    "responses": {"200": {"description": "OK"},
                                  "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/resources/{code}/offline": {
            "parameters": [_ID_PARAM("code")],
            "post": {"tags": ["resources"], "summary": "资源维修下线（新预约与开工会被阻断）",
                     "responses": {"200": {"description": "OK"},
                                   "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/resources/{code}/online": {
            "parameters": [_ID_PARAM("code")],
            "post": {"tags": ["resources"], "summary": "资源恢复上线",
                     "responses": {"200": {"description": "OK"},
                                   "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/resource-units": {
            "get": {
                "tags": ["resources"],
                "summary": "实例列表（可按 resource_code 过滤）",
                "parameters": [{"name": "resource_code", "in": "query", "required": False,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}},
            },
        },
        "/api/v1/resource-units/{id}/maintenance": {
            "parameters": [_ID_PARAM()],
            "post": {"tags": ["resources"], "summary": "单件实例维修/失效",
                     "responses": {"200": {"description": "OK"},
                                   "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/resource-units/{id}/available": {
            "parameters": [_ID_PARAM()],
            "post": {"tags": ["resources"], "summary": "单件实例恢复可用",
                     "responses": {"200": {"description": "OK"},
                                   "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/plans": {
            "get": {"tags": ["plans"], "summary": "计划列表",
                    "responses": {"200": {"description": "OK"}}},
            "post": {
                "tags": ["plans"],
                "summary": "创建计划（执行既有区段互斥校验）",
                "requestBody": {"required": True,
                                "content": {"application/json": {"schema": _PLAN_TIME_BODY}}},
                "responses": {"201": {"description": "已创建"},
                              "409": {"$ref": "#/components/responses/Error409"}},
            },
        },
        "/api/v1/plans/{id}": {
            "parameters": [_ID_PARAM()],
            "get": {"tags": ["plans"], "summary": "计划详情（含预约）",
                    "responses": {"200": {"description": "OK"},
                                  "404": {"$ref": "#/components/responses/Error404"}}},
        },
        "/api/v1/plans/{id}/reservations": {
            "parameters": [_ID_PARAM(), _IDEMP_PARAM],
            "post": {
                "tags": ["plans"],
                "summary": "预约资源（声明资源、数量与占用窗口）",
                "description": "并发重叠窗口竞争时仅一个计划成功；失败返回 409 RESOURCE_EXHAUSTED。",
                "requestBody": {"required": True,
                                "content": {"application/json": {"schema": _RESERVE_BODY}}},
                "responses": {
                    "201": {"description": "预约成功",
                            "headers": {"Idempotency-Replayed":
                                        {"schema": {"type": "boolean"}}}},
                    "409": {"$ref": "#/components/responses/Error409"},
                    "404": {"$ref": "#/components/responses/Error404"}},
            },
        },
        "/api/v1/plans/{id}/readiness": {
            "parameters": [_ID_PARAM()],
            "get": {"tags": ["plans"], "summary": "开工就绪检查（逐项列出失效阻断）",
                    "responses": _READINESS_RESPONSE},
        },
        "/api/v1/plans/{id}/start": {
            "parameters": [_ID_PARAM()],
            "post": {
                "tags": ["plans"],
                "summary": "开工；存在失效项时逐项阻断",
                "responses": {
                    "200": {"description": "已开工"},
                    "409": {"$ref": "#/components/responses/Error409"},
                    "404": {"$ref": "#/components/responses/Error404"}},
            },
        },
        "/api/v1/plans/{id}/release": {
            "parameters": [_ID_PARAM()],
            "post": {
                "tags": ["plans"],
                "summary": "销记释放（幂等，仅释放一次）",
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"$ref": "#/components/responses/Error404"}},
            },
        },
        "/api/v1/plans/{id}/replacements": {
            "parameters": [_ID_PARAM()],
            "post": {
                "tags": ["plans"],
                "summary": "替换失效占用（同资源换实例 / 替代资源），成功后重新计算就绪状态",
                "requestBody": {"required": True,
                                "content": {"application/json": {"schema": _REPLACE_BODY}}},
                "responses": {
                    "200": {"description": "替换完成"},
                    "409": {"$ref": "#/components/responses/Error409"},
                    "404": {"$ref": "#/components/responses/Error404"}},
            },
        },
        "/api/v1/audit": {
            "get": {
                "tags": ["audit"],
                "summary": "审计日志（倒序，分页/过滤）",
                "parameters": [
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer", "default": 100}},
                    {"name": "offset", "in": "query",
                     "schema": {"type": "integer", "default": 0}},
                    {"name": "entity_type", "in": "query", "schema": {"type": "string"}},
                    {"name": "entity_id", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
}
