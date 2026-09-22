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

    function loadImage(image, target, widths, sizes, alt) {
        if (!image || !target) {
            return;
        }
        var widest = widths[widths.length - 2] || widths[widths.length - 1];
        var src = withWidth(target, widest);
        image.setAttribute("srcset", srcsetFor(target, widths));
        image.setAttribute("sizes", sizes);
        if (alt !== undefined) {
            image.setAttribute("alt", alt);
        }
        image.onload = function () {
            image.removeAttribute("hidden");
        };
        image.onerror = function () {
            image.setAttribute("hidden", "hidden");
        };
        image.setAttribute("src", src);
    }

    /* ------------------------------ artwork ------------------------------- */
    function requestArt() {
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

        return fetch(url, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
            signal: controller ? controller.signal : undefined
        })
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

    function applyArt(data) {
        var poster = artPath(data.poster);
        var backdrop = artPath(data.backdrop) || poster;

        if (poster) {
            loadImage(posterImg, poster, POSTER_WIDTHS, POSTER_SIZES, title ? title + " poster" : "");
            if (posterCard) {
                posterCard.classList.add("is-ready");
            }
        }
        if (backdrop) {
            loadImage(backdropImg, backdrop, BACKDROP_WIDTHS, "100vw", "");
            hero.classList.add("is-art");
        }
        setState("ready");
        if (poster && data.has_poster !== false) {
            hideStatus();
        } else {
            /* Either the movie has no poster yet, or the API answered with
               something we refuse to load: the branded placeholder stays. */
            showStatus("Poster coming soon — the bot already has the movie");
        }
    }

    function failArt(error) {
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
