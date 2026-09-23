/* ==========================================================================
   Stream Mode · watch-page movie hero
   Served from the bot's own web server (/static/watch_hero.js).

   The strip above the player is rendered by the server (title, chips, upload
   date, Telegram deep link).  This script only adds the artwork:

   * reads its configuration from the <section data-*> attributes:
         data-art      → artwork endpoint   (default "/api/movies/art")
         data-movie-id → MOVIE_ID           (for the poster/backdrop paths)
         data-title    → title hint for the lookup
         data-year     → year hint for the lookup
         data-deeplink → https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>
   * asks GET /api/movies/art/<MOVIE_ID> for the poster/backdrop URLs and then
     loads the images from our own origin (never from TMDB/IMDb directly, so
     no third party sees the visitor),
   * re-validates every URL it receives – a compromised API can point at
     nothing but our own /api/movies/ endpoints,
   * degrades to the built-in placeholder instead of a broken image, and keeps
     the "Copy search link" / "Play here" buttons working with no artwork at
     all (and with JavaScript disabled the strip is still readable).

   The poster card must never sit blank:

   * `is-ready` is applied only after the image has really loaded – until
     then the branded placeholder (title + icon) keeps the card filled,
   * an image that fails is retried with a fresh `&r=` cache-busting token
     after 1.5 s, 4 s and 9 s; when every attempt failed the placeholder
     stays on screen with a "Poster coming soon" note,
   * a movie uploaded moments ago answers `has_poster: false` – its poster
     is being looked up in the background, so the artwork API is re-checked
     after 8 s and 25 s (bypassing the browser's copy of the earlier
     "no poster yet" answer) and the real poster fills in without a reload.
   ========================================================================== */
(function () {
    "use strict";

    var hero = document.getElementById("movieHero");
    if (!hero) {
        return;
    }

    /* --- config + validation rules (declared before the first use below) --- */
    var POSTER_WIDTHS = [320, 480, 800, 1600];
    var BACKDROP_WIDTHS = [720, 960, 1280, 1920];
    var POSTER_SIZES = "(max-width: 420px) 108px, (max-width: 640px) 124px, (max-width: 900px) 170px, 208px";
    var SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var SAFE_DEEPLINK = /^https:\/\/t\.me\/([A-Za-z0-9_]{4,32})\?start=movie_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var REQUEST_TIMEOUT = 9000;

    /* Poster card retry / re-check schedule (see the file header): */
    var POSTER_RETRY_DELAYS = [1500, 4000, 9000]; /* 1.5 s / 4 s / 9 s */
    var BACKDROP_RETRY_DELAYS = [1500, 4000, 9000];
    var ART_REPOLL_DELAYS = [8000, 25000]; /* has_poster: false → 8 s, 25 s */

    var artEndpoint = (hero.getAttribute("data-art") || "/api/movies/art").trim();
    var movieId = safeId(hero.getAttribute("data-movie-id"));
    var title = safeText(hero.getAttribute("data-title"), 120);
    var year = safeInt(hero.getAttribute("data-year"));
    var deeplink = safeDeeplink(hero.getAttribute("data-deeplink"));

    var posterCard = document.getElementById("mhPosterCard");
    var posterImg = document.getElementById("mhPoster");
    var backdropImg = document.getElementById("mhBackdrop");
    var status = document.getElementById("mhStatus");
    var copyBtn = document.getElementById("mhCopy");
    var playBtn = document.getElementById("mhPlay");

    /* ----------------- retry / re-check machinery (timers) ----------------- */
    /* A generation counter invalidates every pending timer: a newer artwork
       answer (or a manual refresh) always supersedes retries that were
       scheduled by an older one. */
    var generation = 0;
    var pendingTimers = [];
    var posterTarget = null;
    var backdropTarget = null;
    var posterRetries = 0;
    var backdropRetries = 0;

    function clearPendingTimers() {
        pendingTimers.forEach(function (timer) {
            window.clearTimeout(timer);
        });
        pendingTimers = [];
    }

    function bumpGeneration() {
        generation += 1;
        clearPendingTimers();
    }

    function later(callback, delayMs) {
        var gen = generation;
        var timer = window.setTimeout(function () {
            if (gen === generation) {
                callback();
            }
        }, delayMs);
        pendingTimers.push(timer);
        return timer;
    }

    /* A fresh `&r=` token per attempt: browser (and CDN) caches of a failed
       rendition are bypassed, while the server ignores the extra parameter. */
    function withRetryToken(target) {
        if (!target) {
            return target;
        }
        var search = target.search || "";
        return {
            prefix: target.prefix,
            pathname: target.pathname,
            search: (search ? search + "&r=" : "?r=") + String(Date.now())
        };
    }

    /* ------------------------------- helpers ------------------------------- */
    function safeText(value, maxLength) {
        var text = String(value === null || value === undefined ? "" : value);
        text = text.replace(/[\u0000-\u001f\u007f]/g, "").replace(/[<>]/g, "").replace(/\s+/g, " ").trim();
        if (maxLength && text.length > maxLength) {
            text = text.slice(0, maxLength - 1).trim() + "…";
        }
        return text;
    }

    function safeId(value) {
        var id = safeText(value, 64);
        return SAFE_ID.test(id) ? id : "";
    }

    function safeInt(value) {
        var parsed = parseInt(value, 10);
        return isNaN(parsed) || parsed < 1900 || parsed > 2100 ? "" : parsed;
    }

    function safeDeeplink(value) {
        var link = String(value === null || value === undefined ? "" : value).trim();
        return SAFE_DEEPLINK.test(link) ? link : "";
    }

    function setState(state) {
        hero.setAttribute("data-mh-state", state);
    }

    function showStatus(message) {
        if (!status) {
            return;
        }
        status.textContent = safeText(message, 120);
        status.removeAttribute("hidden");
    }

    function hideStatus() {
        if (status) {
            status.setAttribute("hidden", "hidden");
            status.textContent = "";
        }
    }

    function resolveBase(endpoint) {
        try {
            return new URL(endpoint, window.location.href);
        } catch (error) {
            return new URL("/", window.location.href);
        }
    }

    /* Only the artwork endpoints of the configured API may ever be loaded – the
       API answer is treated as untrusted input, exactly like the movie feed. */
    function artPath(path) {
        if (typeof path !== "string" || !path || path.length > 400) {
            return "";
        }
        if (path.charAt(0) !== "/" || /[\\\s]/.test(path)) {
            return "";
        }
        var base = resolveBase(artEndpoint);
        var url;
        try {
            url = new URL(path, base);
        } catch (error) {
            return "";
        }
        if (url.protocol !== "http:" && url.protocol !== "https:") {
            return "";
        }
        if (url.origin !== base.origin || url.pathname.indexOf("/api/movies/") !== 0) {
            return "";
        }
        return { prefix: url.origin, pathname: url.pathname, search: url.search };
    }

    function withWidth(target, width) {
        if (!target) {
            return "";
        }
        var query = target.search || "";
        var joiner = query ? "&" : "?";
        var url = target.pathname + query + joiner + "w=" + width;
        if (title) {
            url += "&q=" + encodeURIComponent(title);
        }
        if (year) {
            url += "&y=" + encodeURIComponent(String(year));
        }
        return target.prefix + url;
    }

    function srcsetFor(target, widths) {
        if (!target) {
            return "";
        }
        return widths
            .map(function (width) {
                return withWidth(target, width) + " " + width + "w";
            })
            .join(", ");
    }

    /* Loads one artwork rendition.  `onReady` fires only after the image
       really loaded – that is the moment the poster card may flip to
       `is-ready`.  `onFail` is the retry hook.  The per-image sequence number
       keeps a slow, superseded request from firing the newer one's handlers. */
    function loadImage(image, target, widths, sizes, alt, onReady, onFail) {
        if (!image || !target) {
            return;
        }
        var sequence = (image._mhSeq || 0) + 1;
        image._mhSeq = sequence;
        var widest = widths[widths.length - 2] || widths[widths.length - 1];
        image.setAttribute("srcset", srcsetFor(target, widths));
        image.setAttribute("sizes", sizes);
        if (alt !== undefined) {
            image.setAttribute("alt", alt);
        }
        image.onload = function () {
            if (image._mhSeq !== sequence) {
                return;
            }
            image.removeAttribute("hidden");
            if (onReady) {
                onReady();
            }
        };
        image.onerror = function () {
            if (image._mhSeq !== sequence) {
                return;
            }
            image.setAttribute("hidden", "hidden");
            if (onFail) {
                onFail();
            }
        };
        image.setAttribute("src", withWidth(target, widest));
    }

    /* ------------------------------ artwork ------------------------------- */
    function requestArt(bypassCache) {
        if (!movieId) {
            throw new Error("No movie id");
        }
        var query = [];
        if (title) {
            query.push("q=" + encodeURIComponent(title));
        }
        if (year) {
            query.push("y=" + encodeURIComponent(String(year)));
        }
        var url =
            artEndpoint.replace(/\/+$/, "") +
            "/" +
            encodeURIComponent(movieId) +
            (query.length ? "?" + query.join("&") : "");

        var controller = typeof AbortController === "function" ? new AbortController() : null;
        var timer = window.setTimeout(function () {
            if (controller) {
                controller.abort();
            }
        }, REQUEST_TIMEOUT);

        var options = {
            credentials: "same-origin",
            headers: { Accept: "application/json" }
        };
        if (bypassCache) {
            /* Re-checks must reach the server: a "no poster yet" answer is
               cached by the browser for a minute (the API's miss TTL), and a
               stale copy would defeat the whole point of the re-check. */
            options.cache = "no-store";
        }
        if (controller) {
            options.signal = controller.signal;
        }

        return fetch(url, options)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Artwork request failed (" + response.status + ")");
                }
                return response.json();
            })
            .then(function (data) {
                window.clearTimeout(timer);
                if (!data || data.ok !== true) {
                    throw new Error("Unexpected artwork payload");
                }
                return data;
            })
            .catch(function (error) {
                window.clearTimeout(timer);
                throw error;
            });
    }

    function dropPosterImage() {
        posterTarget = null;
        if (!posterImg) {
            return;
        }
        posterImg.onload = null;
        posterImg.onerror = null;
        posterImg.removeAttribute("srcset");
        posterImg.removeAttribute("src");
        posterImg.setAttribute("hidden", "hidden");
    }

    function loadPoster(target) {
        posterTarget = target;
        loadImage(
            posterImg,
            target,
            POSTER_WIDTHS,
            POSTER_SIZES,
            title ? title + " poster" : "",
            function () {
                /* The image is on screen – only now may the card turn ready;
                   until this moment the branded placeholder covers it, so the
                   poster card can never sit blank. */
                posterRetries = 0;
                if (posterCard) {
                    posterCard.classList.add("is-ready");
                }
                setState("ready");
                hideStatus();
            },
            function () {
                schedulePosterRetry();
            }
        );
    }

    function schedulePosterRetry() {
        if (!posterTarget) {
            return;
        }
        if (posterRetries >= POSTER_RETRY_DELAYS.length) {
            /* Every attempt failed: the branded placeholder stays on screen. */
            dropPosterImage();
            if (posterCard) {
                posterCard.classList.remove("is-ready");
            }
            setState("ready");
            showStatus("Poster coming soon — the bot already has the movie");
            return;
        }
        var delay = POSTER_RETRY_DELAYS[posterRetries];
        posterRetries += 1;
        later(function () {
            loadPoster(withRetryToken(posterTarget));
        }, delay);
    }

    function loadBackdrop(target) {
        backdropTarget = target;
        loadImage(
            backdropImg,
            target,
            BACKDROP_WIDTHS,
            "100vw",
            "",
            function () {
                backdropRetries = 0;
                hero.classList.add("is-art");
            },
            function () {
                scheduleBackdropRetry();
            }
        );
    }

    function scheduleBackdropRetry() {
        if (!backdropTarget) {
            return;
        }
        if (backdropRetries >= BACKDROP_RETRY_DELAYS.length) {
            backdropTarget = null;
            return; /* the gradient band stays underneath – nothing to say */
        }
        var delay = BACKDROP_RETRY_DELAYS[backdropRetries];
        backdropRetries += 1;
        later(function () {
            loadBackdrop(withRetryToken(backdropTarget));
        }, delay);
    }

    /* A brand-new upload: the artwork API answered "no poster yet".  The
       poster worker runs in the background and usually finishes within a
       few seconds – so ask again at 8 s and at 25 s (both anchored to the
       "no poster yet" answer).  When a poster has arrived, applyArt()
       supersedes the remaining re-check. */
    function scheduleArtRepoll() {
        ART_REPOLL_DELAYS.forEach(function (delayMs, index) {
            var last = index === ART_REPOLL_DELAYS.length - 1;
            later(function () {
                requestArt(true)
                    .then(function (data) {
                        if (!data || data.ok !== true || data.has_poster === false) {
                            throw new Error("No poster yet");
                        }
                        applyArt(data);
                    })
                    .catch(function () {
                        /* Still nothing (or the API hiccupped): the remaining
                           re-checks keep asking; when the last one has also
                           come back empty, the placeholder simply stands. */
                        if (last) {
                            setState("ready");
                        }
                    });
            }, delayMs);
        });
    }

    function applyArt(data) {
        bumpGeneration();
        posterRetries = 0;
        backdropRetries = 0;

        var poster = artPath(data.poster);
        var backdrop = artPath(data.backdrop) || poster;

        /* Poster card – the branded placeholder stays visible until the real
           image has loaded, so the card can never be blank. */
        if (posterCard) {
            posterCard.classList.remove("is-ready");
        }
        if (poster && posterImg && data.has_poster !== false) {
            setState("loading");
            loadPoster(poster);
        } else {
            dropPosterImage();
            showStatus("Poster coming soon — the bot already has the movie");
            if (data.has_poster === false) {
                /* Just uploaded: re-check the artwork API in 8 s / 25 s. */
                setState("loading");
                scheduleArtRepoll();
            } else {
                /* The API answered, but with no (usable) poster – done. */
                setState("ready");
            }
        }

        /* Backdrop band – decorative: the gradient stays underneath, so
           missing wide art never breaks the layout. */
        hero.classList.remove("is-art");
        if (backdrop) {
            loadBackdrop(backdrop);
        }
    }

    function failArt(error) {
        bumpGeneration();
        /* A poster that is already on screen is worth keeping when only a
           refresh failed – never blank out a working card. */
        var keepPoster = posterCard && posterCard.classList.contains("is-ready");
        if (!keepPoster) {
            dropPosterImage();
            if (posterCard) {
                posterCard.classList.remove("is-ready");
            }
        }
        setState("error");
        showStatus(
            "Artwork unavailable right now — tap the button to open it in Telegram"
        );
        if (window.console && window.console.debug) {
            window.console.debug("[movieHero]", error && error.message ? error.message : error);
        }
    }

    setState("loading");
    requestArt().then(applyArt).catch(failArt);

    /* ------------------------------ buttons ------------------------------- */
    function copyLink() {
        if (!deeplink) {
            return Promise.resolve(false);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(deeplink).then(function () {
                return true;
            });
        }
        return new Promise(function (resolve) {
            var box = document.createElement("textarea");
            box.value = deeplink;
            box.setAttribute("readonly", "");
            box.style.position = "fixed";
            box.style.opacity = "0";
            document.body.appendChild(box);
            box.select();
            var ok = false;
            try {
                ok = document.execCommand("copy");
            } catch (error) {
                ok = false;
            }
            document.body.removeChild(box);
            resolve(ok);
        });
    }

    if (copyBtn) {
        copyBtn.addEventListener("click", function () {
            copyLink().then(function (ok) {
                showStatus(ok ? "Search link copied — paste it anywhere" : "Copy failed — press and hold the poster link");
                window.setTimeout(hideStatus, 2600);
            });
        });
    }

    if (playBtn) {
        playBtn.addEventListener("click", function (event) {
            var player = document.getElementById("player");
            if (!player || !player.scrollIntoView) {
                return; /* let the anchor jump to #player */
            }
            event.preventDefault();
            var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            player.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
            if (typeof player.focus === "function") {
                player.focus({ preventScroll: true });
            }
        });
    }

    /* Small public hook (debugging / manual retry from the console). */
    window.MinatoMovieHero = {
        refresh: function () {
            setState("loading");
            return requestArt().then(applyArt).catch(failArt);
        },
        section: hero
    };
})();
