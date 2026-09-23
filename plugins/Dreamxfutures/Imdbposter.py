import re
import aiohttp
import warnings
import logging
from io import BytesIO
from PIL import Image
from info import DREAMXBOTZ_IMAGE_FETCH, TMDB_API_KEY
from imdb import Cinemagoer


logger = logging.getLogger(__name__)
ia = Cinemagoer()
LONG_IMDB_DESCRIPTION = False

def list_to_str(lst):
    if lst:
        return ", ".join(map(str, lst))
    return ""





Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore", Image.DecompressionBombWarning)
async def fetch_image(url, size=(860, 1200)):
    if not DREAMXBOTZ_IMAGE_FETCH:
        logger.info("Image fetching is disabled.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch image: {response.status}")
                    return None

                data = await response.read()
                img = Image.open(BytesIO(data))
                img = img.resize(size, Image.LANCZOS)


                out = BytesIO()
                img.save(out, format="JPEG")
                out.seek(0)
                return out

    except aiohttp.ClientError as e:
        logger.error(f"HTTP request error in fetch_image: {e}")
    except IOError as e:
        logger.error(f"I/O error in fetch_image: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in fetch_image: {e}")

    return None


async def get_movie_details(query, id=False, file=None):
    try:
        if not id:
            query = query.strip().lower()
            title = query
            year = re.findall(r'[1-2]\d{3}$', query, re.IGNORECASE)
            if year:
                year = list_to_str(year[:1])
                title = query.replace(year, "").strip()
            elif file is not None:
                year = re.findall(r'[1-2]\d{3}', file, re.IGNORECASE)
                if year:
                    year = list_to_str(year[:1])
            else:
                year = None
            movieid = ia.search_movie(title.lower(), results=10)
            if not movieid:
                return None
            if year:
                filtered = list(filter(lambda k: str(k.get('year')) == str(year), movieid))
                if not filtered:
                    filtered = movieid
            else:
                filtered = movieid
            movieid = list(filter(lambda k: k.get('kind') in ['movie', 'tv series'], filtered))
            if not movieid:
                movieid = filtered
            movieid = movieid[0].movieID
        else:
            movieid = query
        movie = ia.get_movie(movieid)
        ia.update(movie, info=['main', 'vote details'])
        if movie.get("original air date"):
            date = movie["original air date"]
        elif movie.get("year"):
            date = movie.get("year")
        else:
            date = "N/A"
        plot = movie.get('plot')
        if plot and len(plot) > 0:
            plot = plot[0]
        else:
            plot = movie.get('plot outline')
        if plot and len(plot) > 800:
            plot = plot[:800] + "..."
        poster_url = movie.get('full-size cover url')
        return {
            'title': movie.get('title'),
            'votes': movie.get('votes'),
            "aka": list_to_str(movie.get("akas")),
            "seasons": movie.get("number of seasons"),
            "box_office": movie.get('box office'),
            'localized_title': movie.get('localized title'),
            'kind': movie.get("kind"),
            "imdb_id": f"tt{movie.get('imdbID')}",
            "cast": list_to_str(movie.get("cast")),
            "runtime": list_to_str(movie.get("runtimes")),
            "countries": list_to_str(movie.get("countries")),
            "certificates": list_to_str(movie.get("certificates")),
            "languages": list_to_str(movie.get("languages")),
            "director": list_to_str(movie.get("director")),
            "writer": list_to_str(movie.get("writer")),
            "producer": list_to_str(movie.get("producer")),
            "composer": list_to_str(movie.get("composer")),
            "cinematographer": list_to_str(movie.get("cinematographer")),
            "music_team": list_to_str(movie.get("music department")),
            "distributors": list_to_str(movie.get("distributors")),
            'release_date': date,
            'year': movie.get('year'),
            'genres': list_to_str(movie.get("genres")),
            'poster_url': poster_url,
            'plot': plot,
            'rating': str(movie.get("rating", "N/A")),
            'url': f'https://www.imdb.com/title/tt{movieid}'
        }
    except Exception as e:
        logger.error(f"An error occurred in get_movie_details: {e}")
        return None

async def get_movie_detailsx(query, id=False, file=None):
    """Fetch movie/TV details directly from the TMDB v3 API (no third-party proxy).

    On any failure returns {"error": True} so callers can fall back to IMDB.
    """
    api_key = TMDB_API_KEY
    if not api_key:
        logger.error("TMDB_API_KEY is not set - cannot fetch TMDB details for '%s'", query)
        return {"error": True}

    base = "https://api.themoviedb.org/3"
    img_base = "https://image.tmdb.org/t/p/original"
    params = {"api_key": api_key, "language": "en-US"}

    q = str(query).strip()
    years = re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', q)
    year = years[0] if years else None
    q_no_year = q.replace(year, " ").strip() if year else q
    search_queries = [q] if (not year or q_no_year == q) else [q, q_no_year]

    def pick_best(results):
        if not results:
            return None
        if year:
            for r in results:
                date = r.get("release_date") or r.get("first_air_date") or ""
                if date.startswith(year):
                    return r
        return max(results, key=lambda r: r.get("vote_count") or 0)

    media, kind, data = None, None, None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            if id:
                # Direct TMDB id from internal callers - try movie first, then tv
                for skind in ("movie", "tv"):
                    dp = dict(params)
                    dp["append_to_responses"] = "images,credits"
                    async with session.get(f"{base}/{skind}/{id}", params=dp) as resp:
                        if resp.status == 200:
                            kind = skind
                            media = {"id": id}
                            data = await resp.json()
                            break
                if not media:
                    return {"error": True}
            else:
                for skind in ("movie", "tv"):
                    for sq in search_queries:
                        sp = dict(params)
                        sp["query"] = sq
                        async with session.get(f"{base}/search/{skind}", params=sp) as resp:
                            if resp.status != 200:
                                text = await resp.text()
                                logger.error("TMDB search failed [%s] for query=%s\n %s", resp.status, sq, text)
                                return {"error": True}
                            sdata = await resp.json()
                        hit = pick_best(sdata.get("results") or [])
                        if hit:
                            media, kind = hit, skind
                            break
                    if media:
                        break
                if not media:
                    logger.info("TMDB: no match found for query=%s", q)
                    return {"error": True}

                dp = dict(params)
                dp["append_to_responses"] = "images,credits"
                async with session.get(f"{base}/{kind}/{media['id']}", params=dp) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error("TMDB details failed [%s] for %s/%s\n %s", resp.status, kind, media["id"], text)
                        return {"error": True}
                    data = await resp.json()
    except Exception as e:
        logger.error(f"An error occurred in get_movie_detailsx: {e}")
        return {"error": True}

    # Normalize fields (TMDB v3 response shape)
    credits = data.get("credits") or {}
    crew = credits.get("crew") or []
    release_date = data.get("release_date") or data.get("first_air_date") or ""
    runtime = data.get("runtime")
    if isinstance(runtime, list):
        runtime = runtime[0] if runtime else None

    posters = (data.get("images") or {}).get("posters") or []
    backdrops = (data.get("images") or {}).get("backdrops") or []
    best_poster = max(posters, key=lambda p: (p.get("vote_count") or 0, p.get("width") or 0)) if posters else None
    best_backdrop = max(backdrops, key=lambda b: (b.get("vote_count") or 0, b.get("width") or 0)) if backdrops else None

    details = {
        'title': data.get("title") or data.get("name"),
        'localized_title': data.get("original_title") or data.get("original_name"),
        'year': int(release_date[:4]) if release_date[:4].isdigit() else None,
        'release_date': release_date or None,
        'rating': round(float(data["vote_average"]), 1) if data.get("vote_average") is not None else None,
        'votes': int(data.get("vote_count") or 0),
        'runtime': runtime,
        'certificates': None,
        'tmdb_url': f"https://www.themoviedb.org/{kind}/{data.get('id')}",
        'url': f"https://www.themoviedb.org/{kind}/{data.get('id')}",
        'genres': [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        'languages': [l.get("name") for l in (data.get("spoken_languages") or []) if l.get("name")],
        'countries': [c.get("name") for c in (data.get("production_countries") or []) if c.get("name")],
        'director': [c.get("name") for c in crew if c.get("job") == "Director" and c.get("name")],
        'writer': [c.get("name") for c in crew if c.get("job") == "Writer" and c.get("name")],
        'producer': [c.get("name") for c in crew if c.get("job") == "Producer" and c.get("name")],
        'composer': [c.get("name") for c in crew if c.get("job") in ("Composer", "Original Music Composer") and c.get("name")],
        'cinematographer': [c.get("name") for c in crew if c.get("job") == "Director of Photography" and c.get("name")],
        'cast': [c.get("name") for c in (credits.get("cast") or [])[:10] if c.get("name")],
        'plot': data.get("overview"),
        'tagline': data.get("tagline"),
        'box_office': None,
        'distributors': [],
        'imdb_id': data.get("imdb_id"),
        'tmdb_id': data.get("id"),
        'poster_url': f"{img_base}{best_poster['file_path']}" if best_poster and best_poster.get("file_path") else None,
        'backdrop_url': f"{img_base}{best_backdrop['file_path']}" if best_backdrop and best_backdrop.get("file_path") else None,
    }
    return details

