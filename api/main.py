from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="coverlab-api", version="0.1.0")
DATA_OUT = Path("data/out")


class CreateJobRequest(BaseModel):
    input_path: str
    style: str = "piano"


class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 1000
    guidance_scale: float = 7.5


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: str
    manifest_path: str | None = None


JOBS: dict[str, JobStatus] = {}


@app.post("/jobs", response_model=JobStatus)
def create_job(payload: CreateJobRequest) -> JobStatus:
    job_id = str(uuid.uuid4())
    status = JobStatus(job_id=job_id, status="queued", manifest_path=None)
    JOBS[job_id] = status
    return status


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    status = JOBS.get(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="job not found")
    return status


@app.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str) -> dict[str, list[str]]:
    out_dir = DATA_OUT / job_id
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="job output directory not found")
    files = [str(p) for p in sorted(out_dir.glob("*")) if p.is_file()]
    return {"artifacts": files}


@app.post("/generate", response_model=GenerateResponse)
def generate(payload: GenerateRequest) -> GenerateResponse:
    """Generate music from a text prompt. Compatible with music-platform API."""
    job_id = str(uuid.uuid4())
    status = JobStatus(job_id=job_id, status="queued", manifest_path=None)
    JOBS[job_id] = status
    return GenerateResponse(
        job_id=job_id,
        status="queued",
        message=f"Music generation job queued for prompt: {payload.prompt[:50]}..."
    )
