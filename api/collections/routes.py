"""Collections — ordered groups of requests.

Stub routes (501).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.collections.schemas import (
    AddCollectionItemRequest,
    CollectionItemResponse,
    CollectionListResponse,
    CollectionResponse,
    CreateCollectionRequest,
)
from api.dependencies import AuthContext, require_auth

router = APIRouter(prefix="/v1/collections")


@router.post("", response_model=CollectionResponse, status_code=201)
async def create_collection(
    body: CreateCollectionRequest,
    auth: AuthContext = Depends(require_auth),
) -> CollectionResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    auth: AuthContext = Depends(require_auth),
) -> CollectionListResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> CollectionResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.post("/{collection_id}/items", response_model=CollectionItemResponse, status_code=201)
async def add_item(
    collection_id: UUID,
    body: AddCollectionItemRequest,
    auth: AuthContext = Depends(require_auth),
) -> CollectionItemResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.delete("/{collection_id}/items/{item_id}", status_code=204)
async def remove_item(
    collection_id: UUID,
    item_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> None:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
