import json
import logging
from typing import Literal, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import database, crud, schemas
from db.config import settings
from scrappers import tamil_blasters_scrapper as tb_scrapper
from streaming_providers.exceptions import ProviderException
from streaming_providers.realdebrid.api import router as realdebrid_router
from streaming_providers.realdebrid.utils import get_direct_link_from_realdebrid
from streaming_providers.seedr.api import router as seedr_router
from streaming_providers.seedr.utils import get_direct_link_from_seedr
from utils import crypto, torrent
from utils.parser import generate_catalog_ids

logging.basicConfig(
    format="%(levelname)s::%(asctime)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
    level=settings.logging_level,
)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="resources"), name="static")
TEMPLATES = Jinja2Templates(directory="resources")
headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "max-age=3600, stale-while-revalidate=3600, stale-if-error=604800, public",
}
no_cache_headers = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.on_event("startup")
async def init_db():
    await database.init()


@app.on_event("startup")
async def start_scheduler():
    scheduler = AsyncIOScheduler()
    # scheduler.add_job(tb_scrapper.run_schedule_scrape, CronTrigger(hour="*/3"))
    scheduler.start()
    app.state.scheduler = scheduler


@app.on_event("shutdown")
async def stop_scheduler():
    app.state.scheduler.shutdown(wait=False)


@app.get("/", tags=["home"])
async def get_home(request: Request):
    with open("resources/manifest.json") as file:
        manifest = json.load(file)
    return TEMPLATES.TemplateResponse(
        "html/home.html",
        {
            "request": request,
            "name": manifest.get("name"),
            "version": f"{manifest.get('version')}-{settings.git_rev[:7]}",
            "description": manifest.get("description"),
            "gives": [
                "Tamil Movies & Series",
                "Malayalam Movies & Series",
                "Telugu Movies & Series",
                "Hindi Movies & Series",
                "Kannada Movies & Series",
                "English Movies & Series",
                "Dubbed Movies & Series",
            ],
            "logo": "static/images/mediafusion_logo.png",
        },
    )


@app.get("/favicon.ico")
async def get_favicon():
    return FileResponse(
        "resources/images/mediafusion_logo.png", media_type="image/x-icon"
    )


@app.get("/configure", tags=["configure"])
@app.get("/{secret_str}/configure", tags=["configure"])
async def configure(
    response: Response,
    request: Request,
    user_data: schemas.UserData = Depends(crypto.decrypt_user_data),
):
    response.headers.update(headers)
    response.headers.update(no_cache_headers)
    return TEMPLATES.TemplateResponse(
        "html/configure.html", {"request": request, "user_data": user_data.model_dump()}
    )


@app.get("/manifest.json", tags=["manifest"])
@app.get("/{secret_str}/manifest.json", tags=["manifest"])
async def get_manifest(
    response: Response, user_data: schemas.UserData = Depends(crypto.decrypt_user_data)
):
    response.headers.update(headers)
    response.headers.update(no_cache_headers)
    with open("resources/manifest.json") as file:
        manifest = json.load(file)

    user_catalog_ids = generate_catalog_ids(
        user_data.preferred_movie_languages, user_data.preferred_series_languages
    )
    filtered_catalogs = [
        cat for cat in manifest["catalogs"] if cat["id"] in user_catalog_ids
    ]
    manifest["catalogs"] = filtered_catalogs

    return manifest


@app.get(
    "/{secret_str}/catalog/{catalog_type}/{catalog_id}.json",
    response_model=schemas.Metas,
    response_model_exclude_none=True,
    response_model_by_alias=False,
    tags=["catalog"],
)
@app.get(
    "/catalog/{catalog_type}/{catalog_id}.json",
    response_model=schemas.Metas,
    response_model_exclude_none=True,
    response_model_by_alias=False,
    tags=["catalog"],
)
@app.get(
    "/{secret_str}/catalog/{catalog_type}/{catalog_id}/skip={skip}.json",
    response_model=schemas.Metas,
    response_model_exclude_none=True,
    response_model_by_alias=False,
    tags=["catalog"],
)
@app.get(
    "/catalog/{catalog_type}/{catalog_id}/skip={skip}.json",
    response_model=schemas.Metas,
    response_model_exclude_none=True,
    response_model_by_alias=False,
    tags=["catalog"],
)
async def get_catalog(
    response: Response,
    catalog_type: Literal["movie", "series"],
    catalog_id: str,
    skip: int = 0,
):
    response.headers.update(headers)
    metas = schemas.Metas()
    metas.metas.extend(await crud.get_meta_list(catalog_type, catalog_id, skip))
    return metas


@app.get(
    "/{secret_str}/catalog/{catalog_type}/tamil_blasters/search={search_query}.json",
    response_model=schemas.Metas,
    tags=["search"],
)
@app.get(
    "/catalog/{catalog_type}/tamil_blasters/search={search_query}.json",
    response_model=schemas.Metas,
    tags=["search"],
)
async def search_movie(
    response: Response,
    catalog_type: Literal["movie", "series"],
    search_query: str,
):
    response.headers.update(headers)
    logging.debug("Searching for %s", search_query)

    return await crud.process_search_query(search_query, catalog_type)


@app.get("/{secret_str}/meta/{catalog_type}/{meta_id}.json", tags=["meta"])
@app.get("/meta/{catalog_type}/{meta_id}.json", tags=["meta"])
async def get_meta(
    catalog_type: Literal["movie", "series"], meta_id: str, response: Response
):
    response.headers.update(headers)
    if catalog_type == "movie":
        return await crud.get_movie_meta(meta_id)
    return await crud.get_series_meta(meta_id)


@app.get(
    "/{secret_str}/stream/{catalog_type}/{video_id}.json",
    response_model=schemas.Streams,
    response_model_exclude_none=True,
    tags=["stream"],
)
@app.get(
    "/stream/{catalog_type}/{video_id}.json",
    response_model=schemas.Streams,
    response_model_exclude_none=True,
    tags=["stream"],
)
@app.get(
    "/{secret_str}/stream/{catalog_type}/{video_id}:{season}:{episode}.json",
    response_model=schemas.Streams,
    response_model_exclude_none=True,
    tags=["stream"],
)
@app.get(
    "/stream/{catalog_type}/{video_id}:{season}:{episode}.json",
    response_model=schemas.Streams,
    response_model_exclude_none=True,
    tags=["stream"],
)
async def get_streams(
    catalog_type: Literal["movie", "series"],
    video_id: str,
    response: Response,
    secret_str: str = None,
    season: int = None,
    episode: int = None,
    user_data: schemas.UserData = Depends(crypto.decrypt_user_data),
):
    response.headers.update(headers)

    if catalog_type == "movie":
        fetched_streams = await crud.get_movie_streams(user_data, secret_str, video_id)
    else:
        fetched_streams = await crud.get_series_streams(
            user_data, secret_str, video_id, season, episode
        )

    return {"streams": fetched_streams}


@app.post("/scraper", tags=["scraper"])
def run_scraper(
    background_tasks: BackgroundTasks,
    language: Literal[
        "tamil", "malayalam", "telugu", "hindi", "kannada", "english"
    ] = "tamil",
    video_type: Literal["hdrip", "tcrip", "dubbed", "series"] = "hdrip",
    pages: int = 1,
    start_page: int = 1,
):
    background_tasks.add_task(
        tb_scrapper.run_scraper, language, video_type, pages, start_page
    )
    return {"message": "Scraping in background..."}


@app.post("/encrypt-user-data", tags=["user_data"])
async def encrypt_user_data(user_data: schemas.UserData):
    encrypted_str = crypto.encrypt_user_data(user_data)
    return {"encrypted_str": encrypted_str}


@app.get("/{secret_str}/streaming_provider", tags=["streaming_provider"])
async def streaming_provider_endpoint(
    secret_str: str,
    info_hash: str,
    response: Response,
    season: int = None,
    episode: int = None,
):
    response.headers.update(headers)
    response.headers.update(no_cache_headers)

    user_data = crypto.decrypt_user_data(secret_str)
    if not user_data.streaming_provider:
        raise HTTPException(status_code=400, detail="No streaming provider set.")

    stream = await crud.get_stream_by_info_hash(info_hash)
    if not stream:
        raise HTTPException(status_code=400, detail="Stream not found.")

    magnet_link = torrent.convert_info_hash_to_magnet(
        info_hash, stream.announce_list, stream.torrent_name
    )

    episode_data = stream.get_episode(season, episode)

    try:
        if user_data.streaming_provider.service == "seedr":
            video_url = await get_direct_link_from_seedr(
                info_hash, magnet_link, user_data, stream, episode_data, 3, 1
            )
        else:
            video_url = get_direct_link_from_realdebrid(
                info_hash, magnet_link, user_data, stream, episode_data, 3, 1
            )
    except ProviderException as error:
        logging.info("Exception occurred: %s", error.message)
        video_url = f"{settings.host_url}/static/exceptions/{error.video_file_name}"

    return RedirectResponse(url=video_url, headers=response.headers)


app.include_router(seedr_router, prefix="/seedr", tags=["seedr"])
app.include_router(realdebrid_router, prefix="/realdebrid", tags=["realdebrid"])
