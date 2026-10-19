"""matrag-agent FastAPI 服务（端口 8322）。

Phase B.1 骨架：4 个端点返回 stub 响应，SSE 暂只回放连接与 stub 状态。
- ``POST /agent/dev``   产测软件开发（Phase B.3）
- ``POST /agent/debug`` 调测诊断（Phase B.4）
- ``POST /agent/review`` bundle 评审（Phase B.1）
- ``GET  /agent/{job_id}/events`` 任务事件 SSE（D7：HTTP + SSE）

纪律：不代签；不写 DB；跨仓调用一律经 ``clients/`` 门面。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("matrag_agent.service")

#: 服务端口（独立于 MATRag 8321；spec §5 B.1）。
SERVICE_PORT = 8322

#: 内存任务登记表（Phase B.1 骨架；后续接持久化 / 审计，spec §验证总览）。
_JOBS: dict[str, dict[str, Any]] = {}


class JobRequest(BaseModel):
    """通用任务请求体（骨架：字段宽松，后续按端点细化）。"""

    model_config = ConfigDict(extra="allow")

    product_code: str | None = None
    text: str | None = None
    execution_id: str | None = None
    bundle: dict[str, Any] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动 / 停止日志：明确服务端口与骨架状态。"""
    logger.info("matrag-agent 启动（骨架 Phase B.1），端口 %s", SERVICE_PORT)
    yield
    logger.info("matrag-agent 停止")


app = FastAPI(title="matrag-agent", version="0.1.0", lifespan=lifespan)


def _new_job(kind: str, payload: JobRequest | None) -> dict[str, str]:
    """登记一个 stub 任务，返回 ``{job_id, status}``。"""
    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "stub",
        "request": payload.model_dump() if payload is not None else {},
    }
    logger.info("新建任务 job_id=%s kind=%s", job_id, kind)
    return {"job_id": job_id, "status": "stub"}


@app.post("/agent/dev")
def agent_dev(payload: JobRequest | None = None) -> dict[str, str]:
    """产测软件开发（Phase B.3 落地）；骨架返回 job_id。"""
    return _new_job("dev", payload)


@app.post("/agent/debug")
def agent_debug(payload: JobRequest | None = None) -> dict[str, str]:
    """调测诊断（Phase B.4 落地）；骨架返回 job_id。"""
    return _new_job("debug", payload)


@app.post("/agent/review")
def agent_review(payload: JobRequest | None = None) -> dict[str, str]:
    """bundle 评审（Phase B.1）；骨架返回 job_id。"""
    return _new_job("review", payload)


@app.get("/agent/{job_id}/events")
def agent_events(job_id: str) -> StreamingResponse:
    """任务事件 SSE 流。骨架：回放连接与 stub 状态后结束。"""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"未知 job_id: {job_id}")

    async def _stream() -> AsyncIterator[str]:
        yield ": connected\n\n"
        status = {"job_id": job_id, "status": job["status"]}
        yield f"event: status\ndata: {json.dumps(status, ensure_ascii=False)}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
