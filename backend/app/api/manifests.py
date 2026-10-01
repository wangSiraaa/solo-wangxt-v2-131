from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import DataBlock, UploadManifest
from app.schemas.api import CompleteResult, ManifestCreate, ManifestOut
from app.services import manifests as manifest_service

router = APIRouter(prefix="/manifests", tags=["manifests"])


@router.post("", response_model=ManifestOut, status_code=201)
def create_manifest(payload: ManifestCreate, db: Session = Depends(get_db)):
    try:
        return manifest_service.create_manifest(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[ManifestOut])
def list_manifests(db: Session = Depends(get_db)):
    return db.scalars(select(UploadManifest).order_by(UploadManifest.id.desc())).all()


@router.get("/{manifest_key}", response_model=ManifestOut)
def get_manifest(manifest_key: str, db: Session = Depends(get_db)):
    manifest = manifest_service.get_manifest_by_key(db, manifest_key)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    return manifest


@router.put("/{manifest_key}/blocks/{block_index}", status_code=200)
async def upload_block(
    manifest_key: str,
    block_index: int,
    data: UploadFile = File(..., description="Interleaved little-endian raw block"),
    db: Session = Depends(get_db),
):
    payload = await data.read()
    try:
        block = manifest_service.upload_block(db, manifest_key, block_index, payload)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "block_index": block.block_index,
        "sha256": block.sha256,
        "byte_size": block.byte_size,
        "status": block.status.value,
    }


@router.post("/{manifest_key}/complete", response_model=CompleteResult)
def complete_manifest(manifest_key: str, db: Session = Depends(get_db)):
    try:
        manifest, already = manifest_service.complete_manifest(db, manifest_key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return CompleteResult(manifest=ManifestOut.model_validate(manifest), already_completed=already)


@router.get("/{manifest_key}/blocks")
def list_blocks(manifest_key: str, db: Session = Depends(get_db)):
    manifest = manifest_service.get_manifest_by_key(db, manifest_key)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    blocks = db.scalars(
        select(DataBlock).where(DataBlock.manifest_id == manifest.id).order_by(DataBlock.block_index)
    ).all()
    return [
        {
            "block_index": b.block_index,
            "sha256": b.sha256,
            "byte_size": b.byte_size,
            "status": b.status.value,
            "start_sample": b.start_sample,
            "sample_count": b.sample_count,
            "sample_rate": b.sample_rate,
        }
        for b in blocks
    ]
