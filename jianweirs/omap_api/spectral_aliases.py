"""Typed public API groups backed by the existing scientific adapters."""

from typing import Literal

from fastapi import APIRouter, Query

from . import spectral

MultispectralSource = Literal["sentinel2", "landsat", "hls_s30", "hls_l30"]
HyperspectralSource = Literal["emit", "enmap"]

multispectral_router = APIRouter(prefix="/api/multispectral", tags=["2 · 多光谱科学影像"])
hyperspectral_router = APIRouter(prefix="/api/hyperspectral", tags=["3 · 高光谱科学影像"])


class MultispectralSearchRequest(spectral.SpectralSearchRequest):
    source: MultispectralSource = "sentinel2"


class HyperspectralSearchRequest(spectral.SpectralSearchRequest):
    source: HyperspectralSource = "emit"


class MultispectralSubsetRequest(spectral.SpectralSubsetRequest):
    source: MultispectralSource = "sentinel2"


@multispectral_router.get("/sources")
async def multispectral_sources():
    result = await spectral.sources_endpoint()
    return {"spectral_type": "multispectral", "sources": [source for source in result["sources"] if source["spectral_type"] == "multispectral"]}


@hyperspectral_router.get("/sources")
async def hyperspectral_sources():
    result = await spectral.sources_endpoint()
    return {"spectral_type": "hyperspectral", "sources": [source for source in result["sources"] if source["spectral_type"] == "hyperspectral"]}


@multispectral_router.post("/search")
async def multispectral_search(body: MultispectralSearchRequest):
    return await spectral.search_spectral(body)


@hyperspectral_router.post("/search")
async def hyperspectral_search(body: HyperspectralSearchRequest):
    return await spectral.search_spectral(body)


@multispectral_router.get("/asset")
async def multispectral_asset(source: MultispectralSource, scene_id: str = Query(min_length=1, max_length=220),
                              asset_id: str = Query(min_length=1, max_length=255)):
    return await spectral.asset_endpoint(source, scene_id, asset_id)


@hyperspectral_router.get("/asset")
async def hyperspectral_asset(source: HyperspectralSource, scene_id: str = Query(min_length=1, max_length=220),
                             asset_id: str = Query(min_length=1, max_length=255)):
    return await spectral.asset_endpoint(source, scene_id, asset_id)


@multispectral_router.post("/subset")
async def multispectral_subset(body: MultispectralSubsetRequest):
    return await spectral.subset_spectral(body)
