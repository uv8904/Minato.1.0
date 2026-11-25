from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from aiohttp.http_exceptions import BadStatusLine
from dreamxbotz.Bot import multi_clients, work_loads
from dreamxbotz.server.exceptions import FIleNotFound, InvalidHash
from dreamxbotz.util.custom_dl import ByteStreamer
from dreamxbotz.util.render_template import render_page
from info import MULTI_CLIENT


routes = web.RouteTableDef()

# --------------------------------------------------------------------------- #
# Stream Mode · "Newly Uploaded Movies"
# --------------------------------------------------------------------------- #
# The section's read-only JSON API (`dreamxbotz/server/movie_api.py`) and its
# CSS/JS (`dreamxbotz/server/static_assets.py`) live in their own route tables.
# plugins/__init__.py::web_server() registers them *before* this table, because
# the catch-all "/{path:\S+}" stream route below would otherwise swallow them.
# Docs: docs/NEWLY_UPLOADED_MOVIES.md

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("dreamxbotz")

@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return web.Response(text=await render_page(id, secure_hash), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception:
        logging.exception("Watch page failed for %s", request.path)
        raise web.HTTPInternalServerError(text="watch page failed")

@routes.get(r"/{path:\S+}", allow_head=True)
async def download_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return await media_streamer(request, id, secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception:
        logging.exception("Download request failed for %s", request.path)
        raise web.HTTPInternalServerError(text="stream failed")

class_cache = {}

async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range")
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(id)
    logging.debug("after calling get_file_properties")
    
    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash
    
    file_size = file_id.file_size

    # RFC 7233 range handling. The old split("-") + int() crashed with a 500
    # on legal suffix ranges ("bytes=-500", used by Safari/some players) and
    # on multi-ranges. Malformed/multi ranges are ignored -> full 200 reply.
    from_bytes = 0
    until_bytes = file_size - 1
    is_range = False
    if isinstance(range_header, str):
        m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", range_header)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                from_bytes = int(m.group(1))
                until_bytes = int(m.group(2)) if m.group(2) else file_size - 1
            else:  # suffix range: the LAST N bytes
                from_bytes = max(0, file_size - int(m.group(2)))
            if from_bytes >= file_size or until_bytes < from_bytes:
                return web.Response(
                    status=416,
                    body="416: Range not satisfiable",
                    headers={"Content-Range": f"bytes */{file_size}"},
                )
            until_bytes = min(until_bytes, file_size - 1)
            is_range = True

    chunk_size = 1024 * 1024

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            # guess_type returns (type, encoding); interpolating the tuple into
            # a header produced e.g. "('video/mp4', None)" and broke playback.
            mime_type = (mimetypes.guess_type(file_name)[0] or "application/octet-stream")
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    safe_name = str(file_name).replace('"', "").replace("\r", "").replace("\n", "")
    headers = {
        "Content-Type": f"{mime_type}",
        "Content-Length": str(req_length),
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "Accept-Ranges": "bytes",
    }
    if is_range:
        # Content-Range belongs on 206/416 responses only (RFC 7233).
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"
    return web.Response(status=206 if is_range else 200, body=body, headers=headers)
